#!/usr/bin/env python3
"""WeftCut project.json -> FCPXML 1.13 (feasibility spike).

Mapping (see docs/weftcut-parity-audit.md):
  root composition track 0         -> primary storyline (ref-clips + gaps)
  nested composition                -> compound clip (<media><sequence>)
  linked VideoClip + Audio (A-roll) -> asset-clip (video only) + connected audio lane -1
  other visual tracks i             -> connected clips, lane = i (z-order preserved)
  audio tracks                      -> connected clips, negative lanes
  Position (anchor, comp px, y-down) -> adjust-transform position (% of frame height, y-up)
  crop (normalized source)          -> adjust-crop trim-rect (% of source height)
  keyframes (layer-local)           -> keyframeAnimation (clip source time)
  fades                             -> fadeIn/fadeOut on blend amount / volume
  disabled track / layer            -> enabled="0"
Usage: weftcut_to_fcpxml.py project.json out.fcpxml [--bake] [--name NAME] [--event EVENT]
"""
import json, os, subprocess, sys, urllib.parse, uuid, math
from fractions import Fraction
from xml.sax.saxutils import quoteattr

FPS = Fraction(30000, 1001)          # overwritten from root comp
LOG = []

def frames(us):
    return round(Fraction(us, 1_000_000) * FPS)

def t(us):
    """µs -> frame-quantized FCPXML rational time."""
    n = frames(us)
    if n == 0:
        return "0s"
    fd = 1 / FPS
    v = n * fd
    return f"{v.numerator}/{v.denominator}s"

def tf(n):  # frames -> time string
    if n == 0:
        return "0s"
    v = n / FPS
    return f"{v.numerator}/{v.denominator}s"

def a(**kw):
    return " ".join(f"{k.rstrip('_').replace('__', '-')}={quoteattr(str(v))}" for k, v in kw.items() if v is not None)

# ---------------------------------------------------------------- keyframe eval

def _bez(p1x, p1y, p2x, p2y, x):
    # CSS-style unit bezier: solve x(t)=x, return y(t)
    lo, hi = 0.0, 1.0
    for _ in range(40):
        m = (lo + hi) / 2
        bx = 3 * (1 - m) ** 2 * m * p1x + 3 * (1 - m) * m * m * p2x + m ** 3
        if bx < x: lo = m
        else: hi = m
    m = (lo + hi) / 2
    return 3 * (1 - m) ** 2 * m * p1y + 3 * (1 - m) * m * m * p2y + m ** 3

def evaluate(anim, tus):
    if anim["mode"] == "Static":
        return anim["value"]
    ks = anim["value"]
    if tus <= ks[0]["t_us"]: return ks[0]["value"]
    if tus >= ks[-1]["t_us"]: return ks[-1]["value"]
    for k0, k1 in zip(ks, ks[1:]):
        if k0["t_us"] <= tus < k1["t_us"]:
            span = k1["t_us"] - k0["t_us"]
            u = (tus - k0["t_us"]) / span if span else 1
            seg = k0["segment"]["kind"]
            if seg == "Hold": f = 0.0
            elif seg == "Linear": f = u
            elif seg == "Spline": f = _bez(k0["out"]["x"], k0["out"]["y"], k1["in"]["x"], k1["in"]["y"], u)
            else:
                LOG.append(f"segment {seg} approximated as linear"); f = u
            return k0["value"] + (k1["value"] - k0["value"]) * f
    return ks[-1]["value"]

def interp_for(k0, k1):
    seg = k0["segment"]["kind"]
    if seg in ("Linear", "Hold"): return "linear"
    ox, oy, ix, iy = k0["out"]["x"], k0["out"]["y"], k1["in"]["x"], k1["in"]["y"]
    if abs(ox - oy) < 1e-3 and abs(ix - iy) < 1e-3: return "linear"
    ease_out_of_k0 = oy < ox - 0.05      # slow start
    ease_into_k1 = iy > ix + 0.05        # slow end
    if ease_out_of_k0 and ease_into_k1: return "ease"
    if ease_out_of_k0: return "easeIn"
    if ease_into_k1: return "easeOut"
    return "linear"

def key_times(*anims):
    ts = set()
    for an in anims:
        if an["mode"] == "Keyframed":
            ts.update(k["t_us"] for k in an["value"])
    return sorted(ts)

def keyframes(anims, fmt, dur_us, clip_start_frames, bake):
    """Emit keyframes for a (possibly multi-axis) param. Returns xml or None if static."""
    if all(an["mode"] == "Static" for an in anims):
        return None
    if bake:
        step = max(1, frames(dur_us) // 600)  # cap ~600 keys
        ts = [round(Fraction(n, 1) / FPS * 1_000_000) for n in range(0, frames(dur_us) + 1, step)]
        out = []
        for tus in ts:
            out.append(f'<keyframe {a(time=tf(clip_start_frames + frames(tus)), value=fmt(*[evaluate(an, tus) for an in anims]), curve="linear")}/>')
        return "<keyframeAnimation>" + "".join(out) + "</keyframeAnimation>"
    # semantic: one key per authored time (union across axes); interp from driving axis
    driving = next(an for an in anims if an["mode"] == "Keyframed")
    ks = driving["value"]
    times = key_times(*anims)
    out, last_frame = [], None
    for tus in times:
        # interp of the segment leaving this key on the driving axis
        interp = "linear"
        for k0, k1 in zip(ks, ks[1:]):
            if k0["t_us"] == tus:
                interp = interp_for(k0, k1)
                if k0["segment"]["kind"] == "Hold" and k1["value"] != k0["value"]:
                    # step: hold value until one frame before next key
                    hold_t = round((Fraction(frames(k1["t_us"]) - 1) / FPS) * 1_000_000)
                    times_hold = (hold_t, k0)
                    out.append(("hold", tus, hold_t))
        f = clip_start_frames + frames(tus)
        if f == last_frame:
            continue
        last_frame = f
        out.append(("key", f, fmt(*[evaluate(an, tus) for an in anims]), interp))
    xml = []
    for item in out:
        if item[0] == "key":
            _, f, v, interp = item
            xml.append(f'<keyframe {a(time=tf(f), value=v, interp=interp)}/>')
        else:
            _, tus, hold_t = item
            xml.append(f'<keyframe {a(time=tf(clip_start_frames + frames(hold_t)), value=fmt(*[evaluate(an, tus) for an in anims]), interp="linear", curve="linear")}/>')
    # keep ascending order
    xml.sort(key=lambda s: Fraction(s.split('time="')[1].split('"')[0].rstrip("s")))
    return "<keyframeAnimation>" + "".join(xml) + "</keyframeAnimation>"

def _nonlinear(k0, k1, tol=1e-4):
    """True when the authored bezier departs from the straight chord (numeric, not by handle shape)."""
    ox, oy, ix, iy = k0["out"]["x"], k0["out"]["y"], k1["in"]["x"], k1["in"]["y"]
    return any(abs(_bez(ox, oy, ix, iy, u) - u) > tol for u in (0.1, 0.25, 0.5, 0.75, 0.9))

def keyframes_adaptive(anims, fmt, dur_us, clip_start_frames, step=1):
    """Exact-ish motion for params whose easing FCPXML drops (position/scale/rotation):
    authored keys + a key every `step` frames inside eased Spline segments, all linear
    (curve="linear" so FCP's spatial motion path can't overshoot)."""
    if all(an["mode"] == "Static" for an in anims):
        return None
    ts = set(key_times(*anims))
    for an in anims:
        if an["mode"] != "Keyframed":
            continue
        for k0, k1 in zip(an["value"], an["value"][1:]):
            if k0["segment"]["kind"] == "Spline" and _nonlinear(k0, k1):
                f0, f1 = frames(k0["t_us"]), frames(k1["t_us"])
                for f in range(f0 + step, f1, step):
                    ts.add(round(Fraction(f) / FPS * 1_000_000))
            elif k0["segment"]["kind"] == "Hold" and k1["value"] != k0["value"]:
                ts.add(round(Fraction(frames(k1["t_us"]) - 1) / FPS * 1_000_000))
    out, last = [], None
    for tus in sorted(ts):
        f = clip_start_frames + frames(tus)
        if f == last:
            continue
        last = f
        out.append(f'<keyframe {a(time=tf(f), value=fmt(*[evaluate(an, tus) for an in anims]), curve="linear")}/>')
    return "<keyframeAnimation>" + "".join(out) + "</keyframeAnimation>"

# ---------------------------------------------------------------- media

def tc_start_frames(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream_tags=timecode",
                            "-of", "csv=p=0", path], capture_output=True, text=True, timeout=20)
        tc = next((l.strip() for l in r.stdout.splitlines() if l.strip()), None)
    except Exception:
        tc = None
    if not tc:
        return 0
    drop = ";" in tc
    hh, mm, ss, ff = [int(x) for x in tc.replace(";", ":").split(":")]
    nominal = round(FPS)
    total = ((hh * 60 + mm) * 60 + ss) * nominal + ff
    if drop:
        mins = hh * 60 + mm
        total -= 2 * (mins - mins // 10)
    return total

class Resources:
    def __init__(self, project):
        self.pool = project["media_pool"]
        self.items = []
        self.ids = {}
        self.n = 0
        self.fmt_id = self.nid()

    def nid(self):
        self.n += 1
        return f"r{self.n}"

    def asset(self, mid):
        if mid in self.ids:
            return self.ids[mid]
        m = self.pool[mid]
        rid = self.nid()
        path = m["path_abs"]
        md = m.get("metadata") or {}
        kind = m["kind"]
        start_frames = tc_start_frames(path) if kind == "Video" else 0
        dur = md.get("duration_us") or 0
        attrs = dict(id=rid, name=os.path.splitext(os.path.basename(path))[0],
                     start=tf(start_frames), duration=t(dur) if kind != "Image" else "0s")
        if md.get("video") or kind == "Image":
            attrs["hasVideo"] = "1"
            attrs["videoSources"] = "1"
        if md.get("audio"):
            attrs.update(hasAudio="1", audioSources="1", audioChannels=str(md["audio"].get("channels", 2)),
                         audioRate=str(md["audio"].get("sample_rate", 48000)))
        src = "file://" + urllib.parse.quote(path)
        self.items.append(f'<asset {a(**attrs)}><media-rep {a(kind="original-media", src=src)}/></asset>')
        self.ids[mid] = (rid, start_frames)
        return self.ids[mid]

    def size(self, mid):
        md = self.pool[mid].get("metadata") or {}
        v = md.get("video") or md.get("image") or md
        return v.get("width"), v.get("height")

# ---------------------------------------------------------------- clips

class Ctx:
    def __init__(self, project, bake):
        self.p = project
        self.bake = bake
        self.res = Resources(project)
        self.comp_media = {}   # composition id -> media rid
        self.W = self.H = None
        self.stats = {}

    def bump(self, k, n=1):
        self.stats[k] = self.stats.get(k, 0) + n

def transform_xml(ctx, layer, start_frames, mid):
    pr = layer["params"]
    tr = pr.get("transform")
    dur = layer["t_end_us"] - layer["t_start_us"]
    out = []
    W, H = ctx.W, ctx.H
    if tr:
        if tr["anchor_x"]["value"] != 0.5 or tr["anchor_y"]["value"] != 0.5:
            ctx.bump("unsupported:anchor")
        px, py = tr["position"]["x"], tr["position"]["y"]
        fpos = lambda x, y: f"{(x - W / 2) / H * 100:.4f} {-(y - H / 2) / H * 100:.4f}"
        sx, sy = tr["scale_x"], tr["scale_y"]
        fsc = lambda x, y: f"{x:.5f} {y:.5f}"
        rot = tr["rotation_deg"]
        params = []
        kp = keyframes_adaptive([px, py], fpos, dur, start_frames)
        if kp:
            params.append(f'<param name="position">{kp}</param>'); ctx.bump("keyframed:position")
        ks = keyframes_adaptive([sx, sy], fsc, dur, start_frames)
        if ks:
            params.append(f'<param name="scale">{ks}</param>'); ctx.bump("keyframed:scale")
        kr = keyframes_adaptive([rot], lambda r: f"{-r:.4f}", dur, start_frames)
        if kr:
            params.append(f'<param name="rotation">{kr}</param>'); ctx.bump("keyframed:rotation")
        attrs = dict(position=fpos(evaluate(px, 0), evaluate(py, 0)),
                     scale=fsc(evaluate(sx, 0), evaluate(sy, 0)))
        r0 = evaluate(rot, 0)
        if r0: attrs["rotation"] = f"{-r0:.4f}"
        out.append(("transform", f'<adjust-transform {a(**attrs)}>{"".join(params)}</adjust-transform>'))
    crop = pr.get("crop")
    if crop and mid:
        sw, sh = ctx.res.size(mid)
        if sw and sh:
            k = sw / sh * 100
            l_, r_ = crop["x"] * k, (1 - crop["x"] - crop["w"]) * k
            t_, b_ = crop["y"] * 100, (1 - crop["y"] - crop["h"]) * 100
            out.append(("crop", f'<adjust-crop mode="trim"><trim-rect {a(left=f"{l_:.4f}", top=f"{t_:.4f}", right=f"{r_:.4f}", bottom=f"{b_:.4f}")}/></adjust-crop>'))
            ctx.bump("crop")
        else:
            ctx.bump("crop:missing-source-size")
    if pr.get("flip_h") or pr.get("flip_v"): ctx.bump("unsupported:flip")
    if pr.get("blend_mode") not in (None, "Normal"): ctx.bump("unsupported:blend_mode")
    # opacity + fades
    op = pr.get("opacity")
    fi, fo = pr.get("fade_in_us") or 0, pr.get("fade_out_us") or 0
    if op is not None:
        inner = ""
        ko = keyframes([op], lambda v: f"{v:.4f}", dur, start_frames, ctx.bake)
        if ko:
            inner = ko; ctx.bump("keyframed:opacity")
            if fi or fo: ctx.bump("approx:fade+opacity-keys(fades dropped)")
        elif fi or fo:
            inner = (f'<fadeIn {a(type="linear", duration=t(fi))}/>' if fi else "") + \
                    (f'<fadeOut {a(type="linear", duration=t(fo))}/>' if fo else "")
            ctx.bump("fades:video")
        amount = evaluate(op, 0)
        if inner or amount != 1:
            out.append(("blend", f'<adjust-blend {a(amount=f"{amount:.4f}")}>' +
                        (f'<param name="amount">{inner}</param>' if inner else "") + "</adjust-blend>"))
    if mid:
        out.append(("conform", '<adjust-conform type="none"/>'))
    order = {"crop": 0, "conform": 1, "transform": 2, "blend": 3}
    return "".join(x for _, x in sorted(out, key=lambda kv: order[kv[0]]))

def effects_xml(ctx, layer):
    s = ""
    for e in layer.get("effects", []):
        ctx.bump(f"effect:{e['kind']}:unmapped")
    return s

def volume_xml(ctx, layer):
    pr = layer["params"]
    g = pr.get("gain_db", {"mode": "Static", "value": 0})
    fi, fo = pr.get("fade_in_us") or 0, pr.get("fade_out_us") or 0
    dur = layer["t_end_us"] - layer["t_start_us"]
    inner = ""
    if g["mode"] == "Keyframed":
        ctx.bump("keyframed:gain")
        inner = keyframes([g], lambda v: f"{v:.2f}dB", dur, 0, ctx.bake) or ""
    if fi or fo:
        inner += (f'<fadeIn {a(type="easeIn", duration=t(fi))}/>' if fi else "") + \
                 (f'<fadeOut {a(type="easeOut", duration=t(fo))}/>' if fo else "")
        ctx.bump("fades:audio")
    if pr.get("pan", {}).get("value", 0): ctx.bump("unsupported:pan")
    amt = evaluate(g, 0)
    if not inner and not amt:
        return ""
    return f'<adjust-volume {a(amount=f"{amt:.2f}dB")}>' + (f'<param name="amount">{inner}</param>' if inner else "") + "</adjust-volume>"

def clip_xml(ctx, layer, offset_frames, lane, enabled=True, children=""):
    """One WeftCut layer -> FCPXML clip element placed at offset_frames (parent time)."""
    pr = layer["params"]
    kind = pr["kind"]
    dur = t(layer["t_end_us"] - layer["t_start_us"])
    en = None if (enabled and layer.get("enabled", True)) else "0"
    base = layer.get("label") or (os.path.splitext(os.path.basename(ctx.res.pool[pr["media"]]["path_abs"]))[0] if pr.get("media") in ctx.res.pool else kind)
    name = f"{base} ·{layer['id'][-6:]}"
    if kind == "VideoClip":
        rid, s0 = ctx.res.asset(pr["media"])
        if pr.get("time_map"):
            ctx.bump("retime")
        start = s0 + frames(pr["src_in_us"])
        tmap = ""
        if pr.get("time_map") and pr["time_map"].get("kind") == "Affine":
            r = pr["time_map"]["rate"]; rate = Fraction(r["num"], r["den"])
            d = frames(layer["t_end_us"] - layer["t_start_us"])
            start = round(start / rate)          # clip start is in retimed time
            end = start + d
            tmap = f'<timeMap><timept {a(time="0s", value="0s", interp="linear")}/><timept {a(time=tf(end), value=tf(round(end * rate)), interp="linear")}/></timeMap>'
        body = tmap + transform_xml(ctx, layer, start, pr["media"]) + effects_xml(ctx, layer)
        ctx.bump("clip:video")
        return (f'<asset-clip {a(ref=rid, lane=lane, offset=tf(offset_frames), name=name, start=tf(start), duration=dur, enabled=en)}>'
                f'{body}{children}<audio-channel-source srcCh="1, 2" enabled="0"/></asset-clip>' if ctx.res.pool[pr["media"]].get("metadata", {}).get("audio") else
                f'<asset-clip {a(ref=rid, lane=lane, offset=tf(offset_frames), name=name, start=tf(start), duration=dur, enabled=en)}>{body}{children}</asset-clip>')
    if kind == "ImageOverlay":
        rid, _ = ctx.res.asset(pr["media"])
        start = 0
        body = transform_xml(ctx, layer, start, pr["media"]) + effects_xml(ctx, layer)
        ctx.bump("clip:image")
        return f'<video {a(ref=rid, lane=lane, offset=tf(offset_frames), name=name, start=tf(start), duration=dur, enabled=en)}>{body}</video>'
    if kind == "Audio":
        rid, s0 = ctx.res.asset(pr["media"])
        start = s0 + frames(pr["src_in_us"])
        role = pr.get("role") or "dialogue"
        en2 = "0" if pr.get("mute") else en
        ctx.bump("clip:audio")
        return (f'<asset-clip {a(ref=rid, lane=lane, offset=tf(offset_frames), name=name, start=tf(start), duration=dur, enabled=en2, audioRole=role)}>'
                f'{volume_xml(ctx, layer)}</asset-clip>')
    if kind == "CompositionRef":
        mrid = ctx.comp_media[pr["composition"]]
        start = frames(pr["src_in_us"])
        body = transform_xml(ctx, layer, start, None) + effects_xml(ctx, layer)
        ctx.bump("clip:compound-ref")
        return f'<ref-clip {a(ref=mrid, lane=lane, offset=tf(offset_frames), name=name, start=tf(start), duration=dur, enabled=en)}>{body}{children}</ref-clip>'
    ctx.bump(f"unsupported:layer:{kind}")
    return ""

def build_spine(ctx, comp, connected_tracks, spine_track_idx=0):
    """Spine from one track; everything else connects to the spine item covering its start."""
    tracks = comp["tracks"]
    spine_layers = sorted(tracks[spine_track_idx]["layers"], key=lambda l: l["t_start_us"])
    link_of = {}
    for ln in comp.get("links", []):
        for m in ln["members"]:
            link_of[m] = [x for x in ln["members"] if x != m]
    by_id = {l["id"]: l for tr in tracks for l in tr["layers"]}
    # A-roll pattern: spine holds visuals; linked audio on same track rides as lane -1 child
    visual_spine = [l for l in spine_layers if l["params"]["kind"] != "Audio" or not link_of.get(l["id"])]
    paired_audio = {}
    for l in visual_spine:
        for o in link_of.get(l["id"], []):
            if by_id.get(o, {}).get("params", {}).get("kind") == "Audio":
                paired_audio[l["id"]] = by_id[o]
    items = []  # (start_us, end_us, layer|None)
    cur = 0
    for l in visual_spine:
        if l["t_start_us"] > cur:
            items.append((cur, l["t_start_us"], None))
        if l["t_start_us"] < cur:
            ctx.bump("approx:spine-overlap-moved-to-lane")
            connected_tracks.append((1, {"layers": [l], "enabled": True}))
            continue
        items.append((l["t_start_us"], l["t_end_us"], l))
        cur = l["t_end_us"]
    total = max([cur] + [la["t_end_us"] for _, tr in connected_tracks for la in tr["layers"]])
    if total > cur:
        items.append((cur, total, None))
    # attach connected layers
    attach = {i: [] for i in range(len(items))}
    for lane, tr in connected_tracks:
        for la in tr["layers"]:
            idx = next((i for i, (s, e, _) in enumerate(items) if s <= la["t_start_us"] < e), len(items) - 1)
            attach[idx].append((lane, tr, la))
    xml = []
    for i, (s, e, l) in enumerate(items):
        kids = []
        if l is not None:
            # parent-local time for children: parent's start + (t - s)
            pstart = frames(l["params"].get("src_in_us", 0)) if l["params"]["kind"] == "CompositionRef" \
                else ctx.res.asset(l["params"]["media"])[1] + frames(l["params"].get("src_in_us", 0))
        else:
            pstart = 0
        if l is not None and l["id"] in paired_audio:
            au = paired_audio[l["id"]]
            kids.append(clip_xml(ctx, au, pstart + frames(au["t_start_us"] - s), -1))
            ctx.bump("linked-av-pairs")
        for lane, tr, la in attach[i]:
            kids.append(clip_xml(ctx, la, pstart + frames(la["t_start_us"]) - frames(s), lane, enabled=tr.get("enabled", True)))
        if l is None:
            xml.append(f'<gap {a(offset=t(s), start="0s", duration=tf(frames(e) - frames(s)))}>{"".join(kids)}</gap>')
        else:
            el = clip_xml(ctx, l, frames(s), None, children="".join(kids))
            xml.append(el)
    return "".join(xml), total

def comp_tracks_for_connect(comp, spine_idx):
    """Visual tracks -> positive lanes in z-order; audio-only tracks -> negative lanes."""
    out = []
    vis_lane, aud_lane = 0, -1
    for i, tr in enumerate(comp["tracks"]):
        if i == spine_idx or not tr["layers"]:
            continue
        kinds = {l["params"]["kind"] for l in tr["layers"]}
        if kinds <= {"Audio"}:
            out.append((aud_lane, tr)); aud_lane -= 1
        else:
            vis_lane += 1; out.append((vis_lane, tr))
    return out

def convert(project, bake=False, name=None, event="WeftCut Import"):
    global FPS
    root = project["compositions"][project["root_id"]]
    FPS = Fraction(root["fps"]["num"], root["fps"]["den"])
    ctx = Ctx(project, bake)
    ctx.W, ctx.H = root["width"], root["height"]
    fd = 1 / FPS
    fmt = f'<format {a(id=ctx.res.fmt_id, name=f"FFVideoFormat{ctx.W}x{ctx.H}p2997" if FPS == Fraction(30000,1001) else None, frameDuration=f"{fd.numerator}/{fd.denominator}s", width=ctx.W, height=ctx.H, colorSpace="1-1-1 (Rec. 709)")}/>'
    media_xml = []
    # nested compositions first (compound clips)
    for cid, comp in project["compositions"].items():
        if cid == project["root_id"]:
            continue
        ctx.comp_media[cid] = ctx.res.nid()
    for cid, comp in project["compositions"].items():
        if cid == project["root_id"]:
            continue
        ctx.bump("compound-clip")
        spine_idx = next(i for i, tr in enumerate(comp["tracks"]) if tr["layers"])
        conn = comp_tracks_for_connect(comp, spine_idx)
        # linked audio on spine track is handled as pairs; drop audio from connect set
        spine, total = build_spine(ctx, comp, conn, spine_idx)
        label = comp.get("label") or "Compound"
        media_xml.append(f'<media {a(id=ctx.comp_media[cid], name=label, uid=str(uuid.uuid5(uuid.NAMESPACE_URL, cid)).upper())}>'
                         f'<sequence {a(format=ctx.res.fmt_id, duration=t(total), tcStart="0s", tcFormat="DF", audioLayout="stereo", audioRate="48k")}>'
                         f'<spine>{spine}</spine></sequence></media>')
    conn = comp_tracks_for_connect(root, 0)
    spine, total = build_spine(ctx, root, conn, 0)
    pname = name or project.get("metadata", {}).get("name") or "WeftCut Import"
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<!DOCTYPE fcpxml>', '<fcpxml version="1.13">',
           "<resources>", fmt, *ctx.res.items, *media_xml, "</resources>",
           f'<event {a(name=event)}><project {a(name=pname)}>',
           f'<sequence {a(format=ctx.res.fmt_id, duration=t(total), tcStart="0s", tcFormat="DF", audioLayout="stereo", audioRate="48k")}>',
           f"<spine>{spine}</spine></sequence></project></event></fcpxml>"]
    return "\n".join(xml), ctx.stats

if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    bake = "--bake" in sys.argv
    name = sys.argv[sys.argv.index("--name") + 1] if "--name" in sys.argv else None
    event = sys.argv[sys.argv.index("--event") + 1] if "--event" in sys.argv else "WeftCut Import"
    project = json.load(open(src))
    xml, stats = convert(project, bake, name, event)
    open(dst, "w").write(xml)
    stats.update({f"log:{m}": LOG.count(m) for m in set(LOG)})
    print(json.dumps(stats, indent=1, sort_keys=True))
