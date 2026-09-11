#!/usr/bin/env python3
"""SN 抓拍上传 sidecar：监视 sn_results/，把命中的 SN+图片上传到产测系统。

与识别进程解耦：独立进程；网络故障只重试、写 .uploaded 标记防重传，绝不影响 RTSP 拉流。
纯标准库实现（urllib），板子无需额外装包。
"""
import os, time, json, glob, argparse, uuid
import urllib.request


def iter_pending(results_dir):
    for jpg in sorted(glob.glob(os.path.join(results_dir, "sn_*.jpg"))):
        base = jpg[:-4]
        if os.path.exists(base + ".uploaded"):
            continue
        js = base + ".json"
        if not os.path.exists(js):
            continue
        yield jpg, js, base


def _encode_multipart(fields, jpg_path):
    """手工组 multipart/form-data；避免依赖 requests。"""
    boundary = uuid.uuid4().hex
    crlf = b"\r\n"
    buf = []
    for k, v in fields.items():
        buf.append(b"--" + boundary.encode())
        buf.append(('Content-Disposition: form-data; name="%s"' % k).encode())
        buf.append(b"")
        buf.append(str(v).encode("utf-8"))
    fname = os.path.basename(jpg_path)
    with open(jpg_path, "rb") as f:
        content = f.read()
    buf.append(b"--" + boundary.encode())
    buf.append(('Content-Disposition: form-data; name="image"; filename="%s"' % fname).encode())
    buf.append(b"Content-Type: image/jpeg")
    buf.append(b"")
    body = crlf.join(buf) + crlf + content + crlf + b"--" + boundary.encode() + b"--" + crlf
    return body, "multipart/form-data; boundary=" + boundary


def upload_one(url, jpg, js, timeout):
    with open(js, "r", encoding="utf-8") as f:
        meta = json.load(f)
    fields = {"sn": meta.get("sn", ""), "score": str(meta.get("score", 0.0)), "ts": meta.get("ts", "")}
    body, content_type = _encode_multipart(fields, jpg)
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default="/data/soph_SN/sn_results")
    ap.add_argument("--url", default=os.environ.get("SN_UPLOAD_URL", "http://10.80.40.57:8099/api/v1/captures"))
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--timeout", type=float, default=15.0)
    a = ap.parse_args()
    print("[uploader] watch %s -> %s" % (a.results_dir, a.url), flush=True)
    while True:
        for jpg, js, base in iter_pending(a.results_dir):
            try:
                resp = upload_one(a.url, jpg, js, a.timeout)
                with open(base + ".uploaded", "w") as f:
                    f.write(json.dumps(resp))
                print("[uploader] OK %s -> id=%s" % (os.path.basename(jpg), resp.get("id")), flush=True)
            except Exception as e:
                print("[uploader] retry later %s: %s" % (os.path.basename(jpg), e), flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
