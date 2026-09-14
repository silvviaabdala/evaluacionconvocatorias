import base64, io, json, os, re, zipfile
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify, send_from_directory
import requests as req_lib

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"ok": False, "error": str(e)}), 500

@app.errorhandler(413)
def too_large(e):
    return jsonify({"ok": False, "error": "Archivo demasiado grande."}), 413

try:
    from pdfminer.high_level import extract_text as pdf_extract
    PDF_OK = True
except ImportError:
    PDF_OK = False

def extract_docx(data):
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    parts = []
    for para in root.iter(f"{W}p"):
        line = "".join(n.text or "" for n in para.iter(f"{W}t")).strip()
        if line:
            parts.append(line)
    return "\n".join(parts)

def extract_file(name, data):
    n = name.lower()
    if n.endswith(".pdf"):
        if not PDF_OK:
            raise RuntimeError("PDF no soportado. Usa Word (.docx) o Markdown (.md).")
        return pdf_extract(io.BytesIO(data))
    if n.endswith(".docx") or n.endswith(".doc"):
        return extract_docx(data)
    return data.decode("utf-8", errors="replace")

def clean_json(text):
    text = re.sub(r"```json?\n?", "", text).replace("```", "").strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        text = match.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"No se pudo procesar la respuesta. Intenta de nuevo. ({e})")

def claude_post(api_key, system, user_msg):
    try:
        resp = req_lib.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":api_key,"anthropic-version":"2023-06-01"},
            json={"model":"claude-sonnet-4-6","max_tokens":4000,
                  "system":system,"messages":[{"role":"user","content":user_msg}]},
            timeout=250,
        )
    except req_lib.exceptions.Timeout:
        raise RuntimeError("Tiempo de espera agotado. El CV podría ser muy extenso.")
    except req_lib.exceptions.RequestException as e:
        raise RuntimeError(f"Error de conexión: {e}")
    if not resp.ok:
        try:
            msg = resp.json().get("error", {}).get("message", f"Error {resp.status_code}")
        except Exception:
            msg = f"Error {resp.status_code}"
        raise RuntimeError(msg)
    return resp.json()["content"][0]["text"]

# ── LLAMADA 1: metadatos de la convocatoria ───────────────────────
META_SYSTEM = """Eres experto en convocatorias universitarias de la UNAM.
Lee la convocatoria y extrae su informacion en JSON. Sin texto adicional, sin markdown.
Formato:
{"convocatoria_name":"nombre completo","convocatoria_area":"area o campo","eligibility_requirements":[{"id":"req1","label":"etiqueta corta","desc":"descripcion del requisito segun la convocatoria"}]}"""

def get_conv_meta(api_key, conv_text):
    text = claude_post(api_key, META_SYSTEM,
        f"CONVOCATORIA:\n{conv_text[:8000]}\n\nDevuelve el JSON con nombre, area y requisitos.")
    return clean_json(text)

# ── LLAMADA 2..N: un candidato a la vez ──────────────────────────
CAND_SYSTEM = """Eres evaluador experto de convocatorias universitarias de la UNAM.
Evaluas UN candidato con la rubrica siguiente. Devuelves SOLO JSON valido, sin texto ni markdown.

RUBRICA (100 pts):
1.Produccion(max40): libro=8(cap24), art-intern=3(cap18), art-nac=1.5(cap6), cap=1(cap4), dictamen=50pct.
2.Reconocimiento(max20): SNI1=10, SNI2plus=15, premio=5, estancia-intern-3m=2(cap6).
3.Proyectos(max20): PAPIIT/PAPIME-PI=5, ext-intern-PI=6, ext-nac-PI=4, colaborador=1(cap4), proceso=50pct.
4.Formacion(max10): doctoral-dir=3, posdoc=2, maestria-dir=2, proceso=50pct, lic-dir=0.5.
5.Difusion(max10): ponencia-intern=0.3(cap5), nac=0.1(cap3), evento=0.5(cap3), comite=1(cap3), divulg=hasta2.

REGLAS JSON: Sin saltos de linea dentro de strings. Sin nombres reales. Max 3 items en details y observations.

FORMATO (devuelve solo esto):
{"code":"CODIGO","category":"categoria generica","field":"disciplina generica","sni":"Nivel X","tenure":"X anos","eligibility":{"req1":{"status":"ok","note":"nota"}},"scores":{"produccion":{"score":0,"max":40,"details":["det1","Sub-total: 0/40"]},"reconocimiento":{"score":0,"max":20,"details":["det1","Sub-total: 0/20"]},"proyectos":{"score":0,"max":20,"details":["det1","Sub-total: 0/20"]},"formacion":{"score":0,"max":10,"details":["det1","Sub-total: 0/10"]},"participacion":{"score":0,"max":10,"details":["det1","Sub-total: 0/10"]}},"observations":["Obs 1.","Obs 2.","Obs 3."]}"""

def eval_candidate(api_key, conv_text, req_ids, candidate):
    req_list = " | ".join(req_ids)
    user_msg = (
        f"CONVOCATORIA (extracto):\n{conv_text[:4000]}\n\n"
        f"REQUISITOS A VERIFICAR: {req_list}\n\n"
        f"CANDIDATO {candidate['code']} (CV completo):\n{candidate['text']}\n\n"
        f"Codigo del candidato: {candidate['code']}"
    )
    text = claude_post(api_key, CAND_SYSTEM, user_msg)
    result = clean_json(text)
    result["code"] = candidate["code"]
    return result

# ── Orquestador principal ─────────────────────────────────────────
def call_claude(api_key, conv_text, candidates):
    # 1. Extraer metadatos de la convocatoria
    meta = get_conv_meta(api_key, conv_text)
    req_ids = [r["id"] for r in meta.get("eligibility_requirements", [])]

    # 2. Evaluar cada candidato por separado (CV completo)
    evaluated = []
    for c in candidates:
        result = eval_candidate(api_key, conv_text, req_ids, c)
        # Asegurar que eligibility tiene todos los requisitos
        for req in meta.get("eligibility_requirements", []):
            if req["id"] not in result.get("eligibility", {}):
                result.setdefault("eligibility", {})[req["id"]] = {
                    "status": "warn", "note": "No evaluado"
                }
        evaluated.append(result)

    return {
        "convocatoria_name": meta.get("convocatoria_name", ""),
        "convocatoria_area": meta.get("convocatoria_area", ""),
        "eligibility_requirements": meta.get("eligibility_requirements", []),
        "candidates": evaluated,
    }

@app.route("/")
def index():
    return send_from_directory(os.getcwd(), "index.html")

@app.route("/health")
def health():
    cwd = os.getcwd()
    return jsonify({
        "ok": True,
        "html_exists": os.path.exists(os.path.join(cwd, "index.html")),
        "api_key_set": bool(API_KEY),
    })

@app.route("/analyze", methods=["POST"])
def analyze():
    body = request.get_json(force=True)
    key  = API_KEY or body.get("api_key", "")
    if not key:
        return jsonify({"ok": False, "error": "API Key no configurada."}), 500
    conv_raw = base64.b64decode(body["convocatoria"]["data"])
    conv_txt = extract_file(body["convocatoria"]["name"], conv_raw)
    cands = []
    for c in body["candidates"]:
        text = extract_file(c["name"], base64.b64decode(c["data"]))
        cands.append({"code": c["code"], "text": text})
    result = call_claude(key, conv_txt, cands)
    return jsonify({"ok": True, "data": result})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
