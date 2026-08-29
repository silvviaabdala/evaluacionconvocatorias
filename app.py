import base64, io, json, os, re, zipfile
import xml.etree.ElementTree as ET
import urllib.error, urllib.request
from flask import Flask, request, jsonify, Response

app     = Flask(__name__)
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Ruta absoluta al index.html — funciona sin importar el directorio de trabajo
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

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

SYSTEM_PROMPT = """Eres un evaluador experto del RDDUNJA de la UNAM. Analiza los CVs contra la convocatoria y devuelve ÚNICAMENTE un JSON válido, sin texto adicional ni bloques markdown.

ELEGIBILIDAD:
- req1: Personal académico de carrera TC en UNAM
- req2: Edad (H<=40, M<=43 a la fecha de la convocatoria). Sin fecha de nacimiento: warn
- req3: Antigüedad >= 3 años TC en UNAM
- req1b: Sin nombramiento directivo vigente en UNAM
- req4: Publicaciones de alta calidad académica
- req6: Sin sanciones graves

RÚBRICA (100 pts):
1. Producción académica (máx 40): Libro arbitrado 8 pts c/u (cap 24). Art rev intern 3 pts c/u (cap 18). Art rev nac 1.5 pts c/u (cap 6). Cap libro 1 pt c/u (cap 4). En dictamen/prensa 50%.
2. Reconocimiento (máx 20): SNI-1 10 pts. SNI-2+ 15 pts. Premio inst 5 pts c/u. Estancia intern >=3m 2 pts c/u (cap 6).
3. Proyectos (máx 20): PAPIIT/PAPIME como PI 5 pts c/u. Proy intern como PI 6 pts c/u. Proy nac como PI 4 pts c/u. Colaborador no-PI 1 pt c/u (cap 4). En proceso 50%.
4. Formación RRHH (máx 10): Tesis doctoral dir 3 pts c/u. Asesoría posdoc 2 pts c/u. Maestría dir 2 pts c/u. En proceso 50%. Licenciatura dir 0.5 pts c/u.
5. Participación/difusión (máx 10): Ponencia intern 0.3 pts c/u (cap 5). Nac 0.1 pts c/u (cap 3). Org eventos 0.5 c/u (cap 3). Comité editorial/colegiado 1 pt c/u (cap 3). Divulgación hasta 2 pts.

REGLAS: Usa los códigos proporcionados. NUNCA incluyas nombres reales ni fechas exactas. Observaciones en lenguaje académico natural. Aplica todos los caps.

FORMATO JSON (responde SOLO esto, sin markdown):
{"convocatoria_area":"...","candidates":[{"code":"AF01","category":"...","field":"...","sni":"...","tenure":"...","eligibility":{"req1":{"status":"ok","note":"..."},"req2":{"status":"ok","note":"..."},"req3":{"status":"ok","note":"..."},"req1b":{"status":"ok","note":"..."},"req4":{"status":"ok","note":"..."},"req6":{"status":"ok","note":"..."}},"scores":{"produccion":{"score":0,"max":40,"details":["..."]},"reconocimiento":{"score":0,"max":20,"details":["..."]},"proyectos":{"score":0,"max":20,"details":["..."]},"formacion":{"score":0,"max":10,"details":["..."]},"participacion":{"score":0,"max":10,"details":["..."]}},"observations":["...","...","..."]}]}"""

def call_claude(api_key, conv_text, candidates):
    user_msg = (
        f"CONVOCATORIA:\n{conv_text[:10000]}\n\n" +
        "\n\n---\n\n".join(f"CANDIDATO {c['code']}:\n{c['text'][:8000]}" for c in candidates)
    )
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 8000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        err = json.loads(e.read()).get("error", {})
        raise RuntimeError(err.get("message", f"API error {e.code}"))
    raw = re.sub(r"```json?\n?", "", resp["content"][0]["text"]).replace("```", "").strip()
    return json.loads(raw)

@app.route("/")
def index():
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")

@app.route("/health")
def health():
    return jsonify({"ok": True, "html_exists": os.path.exists(HTML_PATH)})

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        body = request.get_json(force=True)
        key  = API_KEY or body.get("api_key", "")
        if not key:
            return jsonify({"ok": False, "error": "API Key no configurada en el servidor."}), 500
        conv_raw = base64.b64decode(body["convocatoria"]["data"])
        conv_txt = extract_file(body["convocatoria"]["name"], conv_raw)
        cands = []
        for c in body["candidates"]:
            text = extract_file(c["name"], base64.b64decode(c["data"]))
            cands.append({"code": c["code"], "text": text})
        result = call_claude(key, conv_txt, cands)
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
