import subprocess, json, os, re, threading, uuid, shutil, tempfile
from flask import Flask, request, jsonify, send_file, send_from_directory, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "yt_downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

progress_store = {}

def find_bin(name):
    found = shutil.which(name)
    if found: return found
    for p in [f"/usr/bin/{name}", f"/usr/local/bin/{name}", f"/app/.apt/usr/bin/{name}"]:
        if os.path.exists(p): return p
    return None

YTDLP  = find_bin("yt-dlp")
FFMPEG_DIR = os.path.dirname(find_bin("ffmpeg") or "") or None

print(f"yt-dlp : {YTDLP}")
print(f"ffmpeg : {FFMPEG_DIR}")

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/check")
def check():
    return jsonify({"ok": bool(YTDLP), "ffmpeg_ok": bool(FFMPEG_DIR)})

@app.route("/info")
def get_info():
    url = request.args.get("url","").strip()
    if not url: return jsonify({"error":"URL chahiye"}), 400
    if not YTDLP: return jsonify({"error":"yt-dlp not found on server"}), 500

    r = subprocess.run([YTDLP,"--dump-json","--no-playlist","--no-warnings", url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return jsonify({"error":"Video nahi mili.", "detail": r.stderr[-200:]}), 400
    try:
        data = json.loads(r.stdout)
        formats, seen = [], set()
        for f in data.get("formats",[]):
            h = f.get("height"); vc = f.get("vcodec","none"); ext = f.get("ext","")
            if vc != "none" and h and ext in ("mp4","webm"):
                k = f"{h}p"
                if k not in seen:
                    seen.add(k)
                    formats.append({"format_id":str(h),"label":k,"ext":"mp4","type":"video"})
        formats.append({"format_id":"audio","label":"MP3","ext":"mp3","type":"audio"})
        formats.sort(key=lambda x:(0 if x["type"]=="video" else 1,-int(x["label"].replace("p","")) if x["type"]=="video" else 0))
        priority=["1080p","720p","480p","360p","240p","MP3"]; final,done=[],set()
        for p in priority:
            for f in formats:
                if f["label"]==p and p not in done: final.append(f);done.add(p)
        return jsonify({"title":data.get("title","Video"),"channel":data.get("uploader",""),
                        "duration":data.get("duration_string",""),"views":data.get("view_count",0),
                        "thumbnail":data.get("thumbnail",""),"video_id":data.get("id",""),
                        "formats":final,"ffmpeg_ok":bool(FFMPEG_DIR)})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

def do_download(job_id, url, height, is_audio):
    prog = progress_store[job_id]
    out_tmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title)s.%(ext)s")
    extra = ["--ffmpeg-location", FFMPEG_DIR] if FFMPEG_DIR else []

    if is_audio:
        args = [YTDLP,"--no-playlist","-f","bestaudio/best","-x",
                "--audio-format","mp3","--audio-quality","0",
                "--newline","--progress","-o",out_tmpl,"--no-warnings"]+extra+[url]
    else:
        if FFMPEG_DIR:
            fmt=(f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
                 f"/bestvideo[height<={height}]+bestaudio/best[height<={height}]")
        else:
            fmt=f"best[height<={height}][ext=mp4]/best[height<={height}]/best"
        args=[YTDLP,"--no-playlist","-f",fmt,"--merge-output-format","mp4",
              "--newline","--progress","-o",out_tmpl,"--no-warnings"]+extra+[url]

    prog["status"]="downloading"
    try:
        proc=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
    except Exception as e:
        prog["status"]="error"; prog["error"]=str(e); return

    last_fp=None
    for line in proc.stdout:
        line=line.strip()
        m=re.search(r'\[download\]\s+([\d.]+)%',line)
        if m:
            prog["percent"]=min(int(float(m.group(1))),99)
            parts=[]
            s=re.search(r'of\s+([\d.]+\s*\S+iB)',line)
            sp=re.search(r'at\s+([\d.]+\s*\S+iB/s)',line)
            et=re.search(r'ETA\s+(\d+:\d+)',line)
            if s: parts.append(s.group(1))
            if sp: parts.append(sp.group(1))
            if et: parts.append("ETA "+et.group(1))
            prog["info"]=" | ".join(parts)
        if "[Merger]" in line or "Merging" in line:
            prog["percent"]=92;prog["status"]="merging";prog["info"]="Video+Audio merge ho raha hai..."
        if "[ExtractAudio]" in line:
            prog["percent"]=92;prog["status"]="converting";prog["info"]="MP3 convert ho raha hai..."
        if "Destination:" in line:
            fp=line.split("Destination:")[-1].strip()
            if fp: last_fp=fp
    proc.wait()

    if proc.returncode!=0:
        prog["status"]="error"; prog["error"]="Download fail hua."; return

    # Find file
    if last_fp and is_audio:
        base=os.path.splitext(last_fp)[0]
        for ext in [".mp3",".m4a",".webm",".opus"]:
            if os.path.exists(base+ext): last_fp=base+ext; break

    if not last_fp or not os.path.exists(last_fp):
        candidates=[os.path.join(DOWNLOAD_DIR,f) for f in os.listdir(DOWNLOAD_DIR) if f.startswith(job_id)]
        if candidates: last_fp=max(candidates,key=os.path.getmtime)

    if not last_fp or not os.path.exists(last_fp):
        prog["status"]="error"; prog["error"]="File nahi mili."; return

    prog["filepath"]=last_fp; prog["percent"]=100; prog["status"]="done"
    prog["info"]=f"Ready: {os.path.basename(last_fp)}"

@app.route("/start_download")
def start_download():
    url=request.args.get("url","").strip()
    height=request.args.get("format","720")
    is_audio=request.args.get("audio","false")=="true"
    if not url: return jsonify({"error":"URL chahiye"}),400
    if not YTDLP: return jsonify({"error":"yt-dlp not found"}),500
    job_id=str(uuid.uuid4())[:8]
    progress_store[job_id]={"percent":0,"status":"starting","info":"Shuru ho raha hai...","filepath":None,"error":None}
    threading.Thread(target=do_download,args=(job_id,url,height,is_audio),daemon=True).start()
    return jsonify({"job_id":job_id})

@app.route("/progress/<job_id>")
def get_progress(job_id):
    prog=progress_store.get(job_id)
    if not prog: return jsonify({"error":"Job nahi mila"}),404
    return jsonify(prog)

@app.route("/get_file/<job_id>")
def get_file(job_id):
    prog=progress_store.get(job_id)
    if not prog or prog.get("status")!="done": return jsonify({"error":"File tayar nahi"}),400
    fp=prog.get("filepath")
    if not fp or not os.path.exists(fp): return jsonify({"error":"File nahi mili"}),404
    del progress_store[job_id]
    resp=send_file(fp,as_attachment=True)
    # Clean up temp file after sending
    @resp.call_on_close
    def cleanup():
        try: os.remove(fp)
        except: pass
    return resp

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    print(f"Server running on port {port}")
    app.run(host="0.0.0.0",port=port,debug=False)
