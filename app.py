import base64, io, json, os, re, zipfile
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify, Response, send_from_directory
import requests as req_lib

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Buscar index.html en varias ubicaciones posibles
def find_html():
    candidates = [
        os.path.join(os.getcwd(), "index.html"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"),
        "/opt/render/project/src/index.html",
        "index.html",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None

HTML_PATH = find_html()
print(f"CWD: {os.getcwd()}")
print(f"HTML_PATH: {HTML_PATH}")
print(f"Archivos en CWD: {os.listdir(os.getcwd())[:15]}")

@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"ok": False, "error": str(e)}), 500

@app.errorhandler(413)
def too_large(e):
    return jsonify({"ok": False, "error": "Archivo demasiado grande. Usa .md o .txt."}), 413

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

SYSTEM_PROMPT = """Eres evaluador experto de convocatorias universitarias de la UNAM.

PASO 1: Lee la convocatoria y extrae su nombre completo, área y requisitos de elegibilidad.
PASO 2: Evalúa cada CV contra esos requisitos usando la rúbrica fija de abajo.

RÚBRICA (100 pts):
1.Producción(40): libro=8(cap24), art-intern=3(cap18), art-nac=1.5(cap6), cap=1(cap4), dictamen=50%.
2.Reconocimiento(20): SNI1=10, SNI2+=15, premio=5c/u, estancia-intern-3m=2(cap6).
3.Proyectos(20): PAPIIT/PAPIME-PI=5, ext-intern-PI=6, ext-nac-PI=4, colaborador=1(cap4), proceso=50%.
4.FormaciónRRHH(10): doctoral=3, posdoc=2, maestría=2, proceso=50%, lic=0.5.
5.Difusión(10): ponencia-intern=0.3(cap5), nac=0.1(cap3), evento=0.5(cap3), comité=1(cap3), divulg=hasta2.

REGLAS: Códigos dados. Sin nombres reales. Observaciones naturales. Aplica caps.

RESPONDE SOLO este JSON sin texto ni markdown:
{"convocatoria_name":"...","convocatoria_area":"...","eligibility_requirements":[{"id":"req1","label":"...","desc":"..."}],"candidates":[{"code":"AF01","category":"...","field":"...","sni":"...","tenure":"...","eligibility":{"req1":{"status":"ok","note":"..."}},"scores":{"produccion":{"score":0,"max":40,"details":["..."]},"reconocimiento":{"score":0,"max":20,"details":["..."]},"proyectos":{"score":0,"max":20,"details":["..."]},"formacion":{"score":0,"max":10,"details":["..."]},"participacion":{"score":0,"max":10,"details":["..."]}},"observations":["...","...","..."]}]}"""

def call_claude(api_key, conv_text, candidates):
    user_msg = (
        f"CONVOCATORIA:\n{conv_text[:6000]}\n\n" +
        "\n\n---\n\n".join(f"CANDIDATO {c['code']}:\n{c['text'][:5000]}" for c in candidates)
    )
    try:
        resp = req_lib.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":api_key,"anthropic-version":"2023-06-01"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":4000,
                  "system":SYSTEM_PROMPT,"messages":[{"role":"user","content":user_msg}]},
            timeout=280,
        )
    except req_lib.exceptions.Timeout:
        raise RuntimeError("Tiempo de espera agotado. Intenta con CVs más cortos.")
    except req_lib.exceptions.RequestException as e:
        raise RuntimeError(f"Error de conexión: {e}")

    if not resp.ok:
        try:
            err = resp.json().get("error", {})
            msg = err.get("message", f"Error {resp.status_code}")
        except Exception:
            msg = f"Error {resp.status_code}: {resp.text[:200]}"
        raise RuntimeError(msg)

    try:
        content = resp.json()["content"][0]["text"]
    except Exception:
        raise RuntimeError("Respuesta inesperada de la API.")

    raw = re.sub(r"```json?\n?","",content).replace("```","").strip()
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        raw = match.group(0)
    return json.loads(raw)

@app.route("/")
def index():
    if HTML_PATH:
        return send_from_directory(os.path.dirname(HTML_PATH), os.path.basename(HTML_PATH))
    return "<h1>Error: index.html no encontrado</h1><p>Ve a /health para diagnóstico.</p>", 500

@app.route("/health")
def health():
    cwd = os.getcwd()
    try:
        files = os.listdir(cwd)
    except Exception as e:
        files = [str(e)]
    return jsonify({
        "ok": True,
        "html_found": HTML_PATH,
        "cwd": cwd,
        "files_in_cwd": files,
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
