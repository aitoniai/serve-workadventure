"""Render a Tiled .tmj map (orthogonal, CSV/array data) to PNG, optionally with a labelled grid."""
import json, os, sys
from PIL import Image, ImageDraw

FLIP_H, FLIP_V, FLIP_D = 0x80000000, 0x40000000, 0x20000000

def render(tmj_path, out, grid=False, tileset_dir=None, scale=1, skip=("collisions", "start"), areas=None):
    m = json.load(open(tmj_path))
    base = tileset_dir or os.path.dirname(tmj_path)
    tw, th = m["tilewidth"], m["tileheight"]
    W, H = m["width"], m["height"]
    sets = []
    for ts in sorted(m["tilesets"], key=lambda t: t["firstgid"]):
        path = os.path.join(base, ts["image"])
        if not os.path.exists(path):  # map-local tilesets (e.g. the pool table) sit next to the map
            path = os.path.join(os.path.dirname(tmj_path), ts["image"])
        img = Image.open(path).convert("RGBA")
        sets.append((ts["firstgid"], ts["columns"], img))
    def tile(gid):
        raw = gid & ~(FLIP_H | FLIP_V | FLIP_D)
        fg, cols, img = [s for s in sets if s[0] <= raw][-1]
        l = raw - fg; r, c = divmod(l, cols)
        t = img.crop((c * tw, r * th, c * tw + tw, r * th + th))
        if gid & FLIP_D: t = t.transpose(Image.TRANSPOSE)
        if gid & FLIP_H: t = t.transpose(Image.FLIP_LEFT_RIGHT)
        if gid & FLIP_V: t = t.transpose(Image.FLIP_TOP_BOTTOM)
        return t
    canvas = Image.new("RGBA", (W * tw, H * th), (30, 30, 30, 255))
    def walk(layers):
        for l in layers:
            if not l.get("visible", True): continue
            if l["type"] == "group": walk(l["layers"])
            elif l["type"] == "tilelayer" and l["name"] not in skip:
                for i, gid in enumerate(l["data"]):
                    if gid:
                        y, x = divmod(i, W)
                        t = tile(gid); canvas.alpha_composite(t, (x * tw, y * th))
            elif l["type"] == "objectgroup" and grid:
                d = ImageDraw.Draw(canvas)
                for o in l["objects"]:
                    d.rectangle([o["x"], o["y"], o["x"] + o.get("width", 0), o["y"] + o.get("height", 0)], outline=(255, 0, 255, 255), width=2)
                    d.text((o["x"] + 3, o["y"] + 3), o.get("name", ""), fill=(255, 0, 255, 255))
    walk(m["layers"])
    if areas:  # outline the areas of a .wam file
        d = ImageDraw.Draw(canvas)
        for a in json.load(open(areas))["areas"]:
            colour = (0, 220, 255, 255) if any(p["type"] == "livekitRoomProperty" for p in a["properties"]) else \
                     (255, 60, 200, 255) if any(p["type"] == "openWebsite" for p in a["properties"]) else (255, 255, 255, 140)
            d.rectangle([a["x"], a["y"], a["x"] + a["width"] - 1, a["y"] + a["height"] - 1], outline=colour, width=2)
            d.text((a["x"] + 4, a["y"] + 4), a["name"], fill=colour)
    if scale != 1:
        canvas = canvas.resize((int(canvas.width * scale), int(canvas.height * scale)), Image.LANCZOS)
    if grid:
        d = ImageDraw.Draw(canvas)
        s = tw * scale
        for x in range(W):
            for y in range(H):
                d.rectangle([x * s, y * s, x * s + s - 1, y * s + s - 1], outline=(255, 0, 0, 70))
            d.text((x * s + 2, 2), str(x), fill=(255, 255, 0, 255))
        for y in range(H):
            d.text((2, y * s + 2), str(y), fill=(0, 255, 255, 255))
    canvas.save(out)

if __name__ == "__main__":
    # python3 render.py map.tmj out.png [--grid] [--tilesets=DIR] [--scale=0.5] [--areas=map.wam]
    render(sys.argv[1], sys.argv[2], grid="--grid" in sys.argv,
           areas=next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--areas=")), None),
           tileset_dir=next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--tilesets=")), None),
           scale=float(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--scale=")), 1)))
