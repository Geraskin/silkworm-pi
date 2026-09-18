from flask import Flask, request, send_file, render_template_string
import subprocess
import shutil
import time
from pathlib import Path

app = Flask(__name__)

BASE_DIR = Path.home() / "camweb"
IMAGE_PATH = BASE_DIR / "latest.jpg"
BASE_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS = {
    "width": 1640,
    "height": 1232,
    "quality": 93,
    "brightness": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "sharpness": 1.0,
    "ev": 0.0,
    "timeout": 1000,
}

HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Silkworm Pi Camera</title>
<style>
body { font-family: Arial, sans-serif; margin: 20px; background:#f5f5f5; color:#222; }
.wrap { display:grid; grid-template-columns:340px 1fr; gap:20px; align-items:start; }
.card { background:white; border-radius:12px; padding:16px; box-shadow:0 2px 8px rgba(0,0,0,.08); margin-bottom:16px; }
.row { margin:14px 0; }
label { font-weight:600; display:block; margin-bottom:4px; }
input[type=range] { width:100%; }
input[type=number] { width:100%; box-sizing:border-box; padding:6px; }
.value { color:#666; font-size:13px; }
button { width:100%; padding:13px; font-size:17px; font-weight:700; border:0; border-radius:10px; cursor:pointer; }
img { width:100%; height:auto; border-radius:10px; display:block; }
.ok { background:#e9f8ee; padding:10px; border-radius:8px; }
.err { background:#fdeaea; padding:10px; border-radius:8px; white-space:pre-wrap; }
@media (max-width:850px){ .wrap { grid-template-columns:1fr; } }
</style>
</head>
<body>
<h1>Silkworm Pi Camera</h1>
<div class="wrap">

<div>
<div class="card">
<h3>Status</h3>
<div><b>CPU:</b> {{ status.temp }}</div>
<div><b>Power/throttle:</b> {{ status.throttled }}</div>
<div><b>Uptime:</b> {{ status.uptime }}</div>
<div><b>Disk free:</b> {{ status.disk_free }}</div>
<div><b>Last photo:</b> {{ status.last_photo }}</div>
</div>

<div class="card">
<h3>Camera</h3>
{% if message %}<div class="ok">{{ message }}</div>{% endif %}
{% if error %}<div class="err">{{ error }}</div>{% endif %}

<form method="post">
<div class="row">
<label>Width</label>
<input type="number" name="width" min="640" max="3280" value="{{ s.width }}">
</div>

<div class="row">
<label>Height</label>
<input type="number" name="height" min="480" max="2464" value="{{ s.height }}">
</div>

{% for name, title, minv, maxv, step in sliders %}
<div class="row">
<label>{{ title }}</label>
<input type="range" name="{{ name }}" min="{{ minv }}" max="{{ maxv }}" step="{{ step }}"
       value="{{ s[name] }}" oninput="this.nextElementSibling.textContent=this.value">
<div class="value">{{ s[name] }}</div>
</div>
{% endfor %}

<button type="submit">Take photo</button>
</form>
</div>
</div>

<div class="card">
<h3>Latest image</h3>
{% if image_url %}
<img src="{{ image_url }}" alt="Latest photo">
{% else %}
<p>No photo yet.</p>
{% endif %}
</div>

</div>
</body>
</html>
"""

SLIDERS = [
    ("quality", "JPEG quality", 50, 100, 1),
    ("brightness", "Brightness", -1, 1, 0.1),
    ("contrast", "Contrast", 0, 2, 0.1),
    ("saturation", "Saturation", 0, 2, 0.1),
    ("sharpness", "Sharpness", 0, 2, 0.1),
    ("ev", "EV compensation", -4, 4, 0.5),
    ("timeout", "Capture delay (ms)", 100, 5000, 100),
]

def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def cmd_text(cmd, fallback="n/a"):
    try:
        r = run(cmd)
        text = (r.stdout or r.stderr).strip()
        return text or fallback
    except Exception:
        return fallback

def status():
    total, used, free = shutil.disk_usage("/")
    last = "—"
    if IMAGE_PATH.exists():
        last = time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(IMAGE_PATH.stat().st_mtime))
    return {
        "temp": cmd_text(["vcgencmd", "measure_temp"]),
        "throttled": cmd_text(["vcgencmd", "get_throttled"]),
        "uptime": cmd_text(["uptime", "-p"]),
        "disk_free": f"{free / (1024**3):.1f} GB",
        "last_photo": last,
    }

def clamp(v, default, low, high, integer=False):
    try:
        x = float(v)
        if integer:
            x = int(x)
        return max(low, min(high, x))
    except Exception:
        return default

@app.route("/", methods=["GET", "POST"])
def index():
    message = ""
    error = ""

    if request.method == "POST":
        SETTINGS["width"] = int(clamp(request.form.get("width"), SETTINGS["width"], 640, 3280, True))
        SETTINGS["height"] = int(clamp(request.form.get("height"), SETTINGS["height"], 480, 2464, True))
        SETTINGS["quality"] = int(clamp(request.form.get("quality"), SETTINGS["quality"], 50, 100, True))
        SETTINGS["brightness"] = clamp(request.form.get("brightness"), SETTINGS["brightness"], -1, 1)
        SETTINGS["contrast"] = clamp(request.form.get("contrast"), SETTINGS["contrast"], 0, 2)
        SETTINGS["saturation"] = clamp(request.form.get("saturation"), SETTINGS["saturation"], 0, 2)
        SETTINGS["sharpness"] = clamp(request.form.get("sharpness"), SETTINGS["sharpness"], 0, 2)
        SETTINGS["ev"] = clamp(request.form.get("ev"), SETTINGS["ev"], -4, 4)
        SETTINGS["timeout"] = int(clamp(request.form.get("timeout"), SETTINGS["timeout"], 100, 5000, True))

        cmd = [
            "rpicam-still",
            "-n",
            "-o", str(IMAGE_PATH),
            "--width", str(SETTINGS["width"]),
            "--height", str(SETTINGS["height"]),
            "--quality", str(SETTINGS["quality"]),
            "--brightness", str(SETTINGS["brightness"]),
            "--contrast", str(SETTINGS["contrast"]),
            "--saturation", str(SETTINGS["saturation"]),
            "--sharpness", str(SETTINGS["sharpness"]),
            "--ev", str(SETTINGS["ev"]),
            "-t", str(SETTINGS["timeout"]),
        ]

        r = run(cmd)
        if r.returncode == 0 and IMAGE_PATH.exists():
            message = "Photo captured."
        else:
            error = (r.stderr or r.stdout or "Capture failed").strip()

    image_url = None
    if IMAGE_PATH.exists():
        image_url = f"/image?ts={int(IMAGE_PATH.stat().st_mtime)}"

    return render_template_string(
        HTML,
        status=status(),
        s=SETTINGS,
        sliders=SLIDERS,
        image_url=image_url,
        message=message,
        error=error,
    )

@app.route("/image")
def image():
    if not IMAGE_PATH.exists():
        return "No image yet", 404
    return send_file(IMAGE_PATH, mimetype="image/jpeg", max_age=0)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
