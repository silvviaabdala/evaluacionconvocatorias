import base64, io, json, os, re, zipfile
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify, send_from_directory
import requests as req_lib

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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

def clean_json(text):
    text = re.sub(r"```json?\n?", "", text).replace("```", "").strip()
    if not text.startswith("{"):
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
        raise RuntimeError(f"Intenta de nuevo — la respuesta no pudo procesarse. ({e})")

SYSTEM_PROMPT = """Eres evaluador experto de convocatorias universitarias de la UNAM. Devuelves SOLO JSON, sin texto previo, sin markdown, sin explicaciones.

PASO 1: Lee la convocatoria. Extrae nombre, área y requisitos de elegibilidad.
PASO 2: Evalúa cada CV con esta rúbrica:

1.Produccion(max40): libro=8(cap24), art-intern=3(cap18), art-nac=1.5(cap6), cap-libro=1(cap4), en-dictamen=50pct.
2.Reconocimiento(max20): SNI1=10, SNI2plus=15, premio=5, estancia-intern-3m=2(cap6).
3.Proyectos(max20): PAPIIT-PI=5, ext-intern-PI=6, ext-nac-PI=4, colaborador=1(cap4), en-proceso=50pct.
4.Formacion(max10): doctoral-dir=3, posdoc=2, maestria-dir=2, en-proceso=50pct, lic-dir=0.5.
5.Difusion(max10): ponencia-intern=0.3(cap5), nac=0.1(cap3), evento=0.5(cap3), comite=1(cap3), divulgacion=hasta2.

REGLAS ESTRICTAS PARA EL JSON:
- Sin saltos de linea dentro de strings. Cada string en una sola linea.
- Sin comillas dobles dentro de strings. Usa (comillas simples) si citas algo.
- Sin nombres reales de personas.
- Maximo 3 items en arrays de details y observations.
- Scores son numeros enteros o decimales simples, nunca texto."""

JSON_FORMAT = """
{
  "convocatoria_name": "nombre completo",
  "convocatoria_area": "area",
  "eligibility_requirements": [
    {"id": "req1", "label": "etiqueta corta", "desc": "descripcion corta"}
  ],
  "candidates": [
    {
      "code": "AF01",
      "category": "categoria generica",
      "field": "disciplina generica",
      "sni": "Nivel X",
      "tenure": "X anos Y meses",
      "eligibility": {
        "req1": {"status": "ok", "note": "nota corta sin nombre"}
      },
      "scores": {
        "produccion":     {"score": 0, "max": 40, "details": ["detalle 1", "Sub-total: 0/40"]},
        "reconocimiento": {"score": 0, "max": 20, "details": ["detalle 1", "Sub-total: 0/20"]},
        "proyectos":      {"score": 0, "max": 20, "details": ["detalle 1", "Sub-total: 0/20"]},
        "formacion":      {"score": 0, "max": 10, "details": ["detalle 1", "Sub-total: 0/10"]},
        "participacion":  {"score": 0, "max": 10, "details": ["detalle 1", "Sub-total: 0/10"]}
      },
      "observations": ["Observacion 1.", "Observacion 2.", "Observacion 3."]
    }
  ]
}"""

def call_claude(api_key, conv_text, candidates):
    user_msg = (
        f"CONVOCATORIA:\n{conv_text[:5000]}\n\n" +
        "\n\n---\n\n".join(f"CANDIDATO {c['code']}:\n{c['text'][:4000]}" for c in candidates) +
        f"\n\nDevuelve SOLO el JSON con este formato exacto:\n{JSON_FORMAT}"
    )
    try:
        resp = req_lib.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type":"application/json","x-api-key":api_key,"anthropic-version":"2023-06-01"},
            json={
                "model": "claude-sonnet-4-6",  # Sonnet: JSON mas confiable que Haiku
                "max_tokens": 4000,
                "system": SYSTEM_PROMPT,
                "messages": [
                    {"role": "user",      "content": user_msg},
                    {"role": "assistant", "content": "{"},  # Prefill: forzar inicio con {
                ],
            },
            timeout=280,
        )
    except req_lib.exceptions.Timeout:
        raise RuntimeError("Tiempo de espera agotado. Intenta con CVs mas cortos.")
    except req_lib.exceptions.RequestException as e:
        raise RuntimeError(f"Error de conexion: {e}")

    if not resp.ok:
        try:
            msg = resp.json().get("error", {}).get("message", f"Error {resp.status_code}")
        except Exception:
            msg = f"Error {resp.status_code}: {resp.text[:200]}"
        raise RuntimeError(msg)

    try:
        content = resp.json()["content"][0]["text"]
    except Exception:
        raise RuntimeError("Respuesta inesperada de la API.")

    # El prefill ya puso "{", Claude continua desde ahi
    return clean_json("{" + content)

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
