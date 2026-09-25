#!/usr/bin/env python3
"""Generate the office map (office.tmj + office.wam.template) from the official map-starter-kit art.

    python3 maps/office/generate.py [--starter-kit DIR] [--preview preview.png]

Everything is built from the starter kit's tilesets (CC-BY-SA 3.0, WorkAdventure), so the map looks like the
official starter office: walls are auto-tiled, furniture groups are copied from the starter's office.tmj and
single objects are cut out of the tilesets. Edit ROOMS / H_WALLS / V_WALLS / DOORS and layout() below, re-run,
then publish with
    scripts/upload-map.sh maps/office office
Needs Python 3 and Pillow (only for --preview). Without --starter-kit, the pinned starter kit is cloned.
"""
import argparse, json, os, subprocess, sys, tempfile, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
STARTER_KIT_REPO = "https://github.com/workadventure/map-starter-kit.git"
STARTER_KIT_TAG = "v3.3.18"
W, H = 60, 34                      # map size in 32px tiles (building 0-43, garden 44-59)
COLLIDE, START = 3, 2              # WA_Special_Zones tiles used by the starter kit

# ── Floors (WA_Room_Builder) ───────────────────────────────────────────────
WOOD, NAVY, RED_CARPET, LIGHT_WOOD, PURPLE = 725, 737, 762, 731, 760
SLATE, HONEY_WOOD = 735, 753
# ── Outdoor ground (WA_Exterior) ──
GRASS, GRASS_VARIANTS = 2461, (2462, 2461, 2461, 2461)
COBBLE, WATER = 2483, 2509

# ── Rooms: name -> (x1, y1, x2, y2) inclusive floor rectangle, floor tile ──
ROOMS = {
    "Meeting room 1": ((1, 1, 9, 11), NAVY),
    "Meeting room 2": ((11, 1, 19, 11), SLATE),
    "Meeting room 3": ((21, 1, 29, 11), HONEY_WOOD),
    "Team desks":     ((31, 1, 42, 11), WOOD),
    "Open office":    ((1, 13, 42, 23), WOOD),
    "Coffee corner":  ((1, 25, 13, 32), LIGHT_WOOD),
    "Reception":      ((15, 25, 28, 32), RED_CARPET),
    "Games area":     ((30, 25, 42, 32), PURPLE),
    "Garden":         ((44, 0, 59, 33), GRASS),
}
MEETING_ROOMS = (1, 11, 21)        # left x of each meeting room

# ── Walls: horizontal runs (y, x1, x2) and vertical runs (x, y1, y2); gaps are doors ──
H_WALLS = [(0, 0, 43), (33, 0, 43), (12, 0, 43), (24, 0, 43)]
V_WALLS = [(0, 0, 33), (43, 0, 33), (10, 0, 12), (20, 0, 12), (30, 0, 12), (14, 24, 33), (29, 24, 33)]
GARDEN_DOORS = [(43, 5, 43, 7), (43, 17, 43, 19), (43, 28, 43, 30)]
DOORS = [  # cells left open (x1, y1, x2, y2)
    (7, 12, 8, 12), (17, 12, 18, 12), (27, 12, 28, 12),        # meeting room doors
    (38, 12, 40, 12),                                          # team desks
    (5, 24, 6, 24), (26, 24, 27, 24), (33, 24, 34, 24),        # coffee, reception, games from the office
    (14, 28, 14, 30), (29, 28, 29, 30),                        # reception <-> coffee / games
] + GARDEN_DOORS                                               # out to the garden


def wall_cells():
    cells = set()
    for y, x1, x2 in H_WALLS:
        cells |= {(x, y) for x in range(x1, x2 + 1)}
    for x, y1, y2 in V_WALLS:
        cells |= {(x, y) for y in range(y1, y2 + 1)}
    for x1, y1, x2, y2 in DOORS:
        cells -= {(x, y) for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)}
    return cells


# Wall pieces by connected sides (measured from WA_Room_Builder.png).
WALL_PIECE = {"": 481, "E": 433, "W": 434, "N": 431, "S": 406, "ES": 403, "SW": 404, "NE": 428, "NW": 429,
              "EW": 479, "NS": 477, "ESW": 535, "NES": 483, "NEW": 485, "NSW": 533, "NESW": 512}
FACE = (578, 603)          # white wall face under a wall (2 tiles tall)
FACE_END = (655, 680)      # face under the bottom end of a vertical wall


class Map:
    def __init__(self, starter):
        self.src = json.load(open(os.path.join(starter, "office.tmj")))
        self.starter = starter
        self.layers = {n: [0] * (W * H) for n in (
            "start", "collisions", "floor1", "floor2", "walls1", "walls2",
            "furniture1", "furniture2", "furniture3", "above1", "above2")}
        self.src_layers = {}
        def walk(ls):
            for l in ls:
                if l["type"] == "tilelayer": self.src_layers[l["name"]] = l["data"]
                elif l["type"] == "group": walk(l["layers"])
        walk(self.src["layers"])
        self._tiles_cache = {}

    def set(self, layer, x, y, gid):
        if 0 <= x < W and 0 <= y < H: self.layers[layer][y * W + x] = gid

    def get(self, layer, x, y):
        return self.layers[layer][y * W + x] if 0 <= x < W and 0 <= y < H else 0

    def fill(self, layer, x1, y1, x2, y2, gid):
        for y in range(y1, y2 + 1):
            for x in range(x1, x2 + 1): self.set(layer, x, y, gid)

    # Copy a rectangle of the starter office (all furniture layers + collisions).
    def stamp(self, sx, sy, w, h, x, y, layers=("walls2", "furniture1", "furniture2", "furniture3", "above1", "above2", "collisions")):
        sw = self.src["width"]
        for dy in range(h):
            for dx in range(w):
                for name in layers:
                    g = self.src_layers[name][(sy + dy) * sw + sx + dx]
                    if g: self.set(name, x + dx, y + dy, g)

    # A multi-tile object cut out of a tileset: flood from a seed tile across seams where art continues.
    def obj(self, seed):
        if seed in self._tiles_cache: return self._tiles_cache[seed]
        from PIL import Image
        ts = [t for t in sorted(self.src["tilesets"], key=lambda t: t["firstgid"]) if t["firstgid"] <= seed][-1]
        img = Image.open(os.path.join(self.starter, ts["image"])).convert("RGBA")
        cols, rows = ts["columns"], img.height // 32
        alpha = img.getchannel("A").load()
        def opaque(px, py): return alpha[px, py] > 40
        def seam(c, r, dc, dr):  # does art continue from tile (c,r) into its neighbour?
            if not (0 <= c + dc < cols and 0 <= r + dr < rows): return False
            hits = 0
            for i in range(32):
                if dc: a, b = (c * 32 + (31 if dc > 0 else 0), r * 32 + i), (c * 32 + (32 if dc > 0 else -1), r * 32 + i)
                else: a, b = (c * 32 + i, r * 32 + (31 if dr > 0 else 0)), (c * 32 + i, r * 32 + (32 if dr > 0 else -1))
                hits += opaque(*a) and opaque(*b)
            return hits >= 3
        l = seed - ts["firstgid"]; c0, r0 = l % cols, l // cols
        seen, todo = {(c0, r0)}, [(c0, r0)]
        while todo:
            c, r = todo.pop()
            for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (c + dc, r + dr)
                if n not in seen and seam(c, r, dc, dr) and abs(n[0] - c0) < 6 and abs(n[1] - r0) < 6:
                    seen.add(n); todo.append(n)
        cmin, rmin = min(c for c, _ in seen), min(r for _, r in seen)
        cmax, rmax = max(c for c, _ in seen), max(r for _, r in seen)
        grid = [[ts["firstgid"] + r * cols + c if (c, r) in seen else 0 for c in range(cmin, cmax + 1)]
                for r in range(rmin, rmax + 1)]
        self._tiles_cache[seed] = grid
        return grid

    def put(self, seed, x, y, layer="furniture1", collide="all", above_rows=0):
        """Place an object (seed tile id, or an explicit grid of tile ids) with its top-left at (x, y).
        collide: all | base (bottom row) | none."""
        grid = seed if isinstance(seed, list) else self.obj(seed)
        for dy, row in enumerate(grid):
            for dx, g in enumerate(row):
                if not g: continue
                self.set("above1" if dy < above_rows else layer, x + dx, y + dy, g)
                if collide == "all" or (collide == "base" and dy == len(grid) - 1):
                    self.set("collisions", x + dx, y + dy, COLLIDE)
        return len(grid[0]), len(grid)

    def build_walls(self):
        walls = wall_cells()
        for (x, y) in walls:
            conn = "".join(d for d, (dx, dy) in (("N", (0, -1)), ("E", (1, 0)), ("S", (0, 1)), ("W", (-1, 0)))
                           if (x + dx, y + dy) in walls)
            self.set("walls1", x, y, WALL_PIECE[conn])
            self.set("collisions", x, y, COLLIDE)
        for (x, y) in walls:  # 2-tile wall face below every wall that has floor under it
            if y == H - 1 or (x, y + 1) in walls: continue
            face = FACE_END if WALL_PIECE_OF(walls, x, y) == "N" else FACE
            for i, g in enumerate(face):
                if (x, y + 1 + i) in walls or y + 1 + i >= H: break
                self.set("walls1", x, y + 1 + i, g)
                self.set("collisions", x, y + 1 + i, COLLIDE)

    def pool_firstgid(self):
        last = max(self.src["tilesets"], key=lambda t: t["firstgid"])
        return last["firstgid"] + last["tilecount"]

    def pool_tileset(self):
        return {"columns": POOL_COLS, "firstgid": self.pool_firstgid(), "image": POOL_TILESET,
                "imageheight": POOL_ROWS * 32, "imagewidth": POOL_COLS * 32, "margin": 0, "name": "Custom_Pool_Table",
                "spacing": 0, "tilecount": POOL_COLS * POOL_ROWS, "tileheight": 32, "tilewidth": 32}

    def game_firstgid(self):
        return self.pool_firstgid() + POOL_COLS * POOL_ROWS

    def game_tileset(self):
        n = len(GAMES)
        return {"columns": n, "firstgid": self.game_firstgid(), "image": GAME_TILESET,
                "imageheight": 32, "imagewidth": n * 32, "margin": 0, "name": "Custom_Game_Tables",
                "spacing": 0, "tilecount": n, "tileheight": 32, "tilewidth": 32}

    def tmj(self):
        def tl(name, i):
            return {"data": self.layers[name], "height": H, "width": W, "id": i, "name": name, "opacity": 1,
                    "type": "tilelayer", "visible": True, "x": 0, "y": 0}
        ids = iter(range(1, 100))
        def group(name, names):
            return {"id": next(ids), "name": name, "opacity": 1, "type": "group", "visible": True, "x": 0, "y": 0,
                    "layers": [tl(n, next(ids)) for n in names]}
        layers = [tl("start", next(ids)), tl("collisions", next(ids)),
                  group("floor", ["floor1", "floor2"]), group("walls", ["walls1", "walls2"]),
                  group("furniture", ["furniture1", "furniture2", "furniture3"]),
                  {"id": next(ids), "name": "floorLayer", "type": "objectgroup", "draworder": "topdown", "objects": [],
                   "opacity": 1, "visible": True, "x": 0, "y": 0},
                  group("above", ["above1", "above2"])]
        for l in layers:
            if l["name"] == "collisions": l["visible"] = False
        return {
            "compressionlevel": -1, "height": H, "width": W, "infinite": False, "orientation": "orthogonal",
            "renderorder": "right-down", "tiledversion": self.src.get("tiledversion", "1.10.2"), "tileheight": 32,
            "tilewidth": 32, "type": "map", "version": self.src.get("version", "1.10"), "nextlayerid": 100,
            "nextobjectid": 1, "layers": layers, "tilesets": self.src["tilesets"] + [self.pool_tileset(), self.game_tileset()],
            "properties": [
                {"name": "mapName", "type": "string", "value": "Office"},
                {"name": "mapDescription", "type": "string",
                 "value": "Reception, coffee corner, games area, open office, three meeting rooms and a garden."},
                {"name": "mapCopyright", "type": "string",
                 "value": "Tiles: WorkAdventure map-starter-kit (https://WorkAdventu.re), CC-BY-SA 3.0 "
                          "(http://creativecommons.org/licenses/by-sa/3.0/)"},
            ],
        }


# ── Pool table: not in the starter kit, so it is drawn here as a small 4x3-tile tileset ──
POOL_TILESET = "tilesets/Custom_Pool_Table.png"
POOL_COLS, POOL_ROWS = 4, 3


def draw_pool_table(path):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (POOL_COLS * 32, POOL_ROWS * 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink, wood, wood_hi, wood_dk = (46, 26, 14), (122, 72, 36), (160, 102, 54), (86, 48, 22)
    felt, cushion, pocket = (46, 142, 76), (30, 106, 56), (18, 18, 18)
    d.ellipse((6, 84, 122, 95), fill=(0, 0, 0, 60))                       # shadow
    for lx in (10, 110):                                                   # legs
        d.rectangle((lx, 78, lx + 7, 90), fill=wood_dk, outline=ink)
    d.rectangle((2, 70, 125, 80), fill=wood_dk, outline=ink)               # front skirt
    d.rounded_rectangle((1, 4, 126, 74), radius=6, fill=wood, outline=ink)  # rail
    d.line((6, 6, 121, 6), fill=wood_hi, width=2)
    d.rectangle((10, 13, 117, 65), fill=cushion)
    d.rectangle((13, 16, 114, 62), fill=felt)
    d.line((36, 16, 36, 62), fill=(62, 160, 92))                           # head string
    for cx, cy in ((11, 14), (64, 12), (116, 14), (11, 64), (64, 66), (116, 64)):
        d.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), fill=pocket, outline=ink)
    d.ellipse((29, 36, 36, 43), fill=(250, 250, 245), outline=ink)         # cue ball
    colours = [(240, 200, 40), (40, 80, 200), (210, 40, 40), (120, 40, 150), (240, 120, 30),
               (30, 130, 60), (130, 30, 30), (20, 20, 20), (240, 200, 40), (40, 80, 200),
               (210, 40, 40), (120, 40, 150), (240, 120, 30), (30, 130, 60), (130, 30, 30)]
    i = 0
    for col in range(5):                                                   # the rack
        for row in range(col + 1):
            x, y = 80 + col * 6, 39 - col * 3.5 + row * 7
            d.ellipse((x - 3, y - 3, x + 3, y + 3), fill=colours[i], outline=ink)
            i += 1
    d.line((8, 52, 30, 42), fill=(214, 170, 110), width=2)                 # cue
    d.line((8, 52, 14, 49), fill=ink, width=2)
    img.save(path)


# ── Game tables: one 32x32 table per entry of GAMES, with that game on its top, so you can see what each plays ──
GAME_TILESET = "tilesets/Custom_Game_Tables.png"


def draw_game_tables(path):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (len(GAMES) * 32, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink, wood, wood_hi, wood_dk = (46, 26, 14), (122, 72, 36), (160, 102, 54), (86, 48, 22)
    for i in range(len(GAMES)):                                            # the same wooden table under each game
        o = i * 32
        d.ellipse((o + 3, 27, o + 28, 31), fill=(0, 0, 0, 60))              # shadow
        for lx in (4, 25):                                                 # legs
            d.rectangle((o + lx, 23, o + lx + 2, 30), fill=wood_dk, outline=ink)
        d.rectangle((o + 1, 21, o + 30, 25), fill=wood_dk, outline=ink)     # front edge
        d.rounded_rectangle((o + 1, 1, o + 30, 22), radius=3, fill=wood, outline=ink)
        d.line((o + 4, 2, o + 27, 2), fill=wood_hi)

    # Chess: 8x8 board with a white and a black king.
    o = 0
    d.rectangle((o + 6, 2, o + 25, 21), fill=(64, 40, 22), outline=ink)
    for r in range(8):
        for c in range(8):
            colour = (238, 220, 180) if (r + c) % 2 == 0 else (150, 100, 60)
            d.rectangle((o + 8 + c * 2, 4 + r * 2, o + 9 + c * 2, 5 + r * 2), fill=colour)
    for kx, body, edge in ((11, (250, 250, 245), ink), (19, (30, 30, 30), (200, 200, 200))):
        d.rectangle((o + kx, 12, o + kx + 2, 17), fill=body, outline=edge)   # king
        d.line((o + kx + 1, 9, o + kx + 1, 11), fill=edge)                   # cross
        d.line((o + kx, 10, o + kx + 2, 10), fill=edge)

    # Pictionary: a sketch of a house under the sun, and a pencil.
    o = 32
    d.rectangle((o + 4, 3, o + 23, 20), fill=(250, 250, 245), outline=(120, 120, 120))
    d.ellipse((o + 17, 5, o + 21, 9), fill=(250, 200, 40), outline=(220, 150, 20))
    d.rectangle((o + 8, 12, o + 15, 18), outline=(40, 80, 200))           # house
    d.polygon([(o + 7, 12), (o + 11, 7), (o + 16, 12)], outline=(210, 40, 40))
    d.rectangle((o + 11, 15, o + 12, 18), fill=(40, 80, 200))             # door
    d.line((o + 25, 19, o + 28, 5), fill=(240, 190, 40), width=2)          # pencil
    d.line((o + 28, 5, o + 28, 3), fill=(240, 140, 160), width=2)
    d.point((o + 25, 20), fill=ink)

    # Codenames: a 5x4 grid of red, blue, beige and one black agent card.
    o = 64
    cards = ["RBNRB", "NRKBN", "BNRRB", "RBNBR"]
    tones = {"R": (210, 50, 40), "B": (40, 90, 200), "N": (230, 210, 170), "K": (25, 25, 25)}
    for r, row in enumerate(cards):
        for c, k in enumerate(row):
            x, y = o + 4 + c * 5, 3 + r * 5
            d.rectangle((x, y, x + 3, y + 3), fill=tones[k], outline=ink)
    img.save(path)


def pool_grid(firstgid):
    return [[firstgid + r * POOL_COLS + c for c in range(POOL_COLS)] for r in range(POOL_ROWS)]


def WALL_PIECE_OF(walls, x, y):
    return "".join(d for d, (dx, dy) in (("N", (0, -1)), ("E", (1, 0)), ("S", (0, 1)), ("W", (-1, 0)))
                   if (x + dx, y + dy) in walls)


# ── Starter-kit furniture groups (source rectangles in its office.tmj) ─────
POD = (10, 3, 4, 5)            # 4 desks with computers and chairs
LONG_DESK = (12, 9, 6, 5)      # row of 3 desks, chairs on both sides
MEETING_TABLE = (25, 7, 4, 7)  # glass table with 8 chairs
WHITEBOARD = (25, 1, 3, 3)
WINDOW = (11, 1, 2, 2)
CLOCK = (14, 1, 1, 1)
LOGO = (1, 1, 6, 1)
KITCHEN = (1, 8, 2, 5)         # counter with coffee machine and pastries
LOUNGE = (1, 3, 6, 4)          # sofa, armchairs and coffee table
SHELF = (6, 6, 2, 2)
PRINTER = (20, 12, 1, 2)
BIN = (21, 13, 1, 1)
ARMCHAIRS = (5, 15, 1, 3)      # two armchairs with a small round table
PALM = (0, 14, 3, 3)

# ── Single objects (seed tile ids; the rest of the object is found automatically) ──
SOFA_BLUE, SOFA_YELLOW, POUF_ORANGE = [[1378, 1379, 1380], [1391, 1392, 1393]], 1443, 1450
SOFA_BEIGE, ARMCHAIR_BEIGE = [[1375, 1376, 1377], [1388, 1389, 1390]], 1416
STOOLS = [1538, 1539, 1540, 1541]
STOOL_WOOD = 1526
TABLE_ROUND_ORANGE, TABLE_ROUND_WHITE, TABLE_ROUND_BLACK, TABLE_BIG = 1697, 1688, 1707, 1767
COUNTER = 1663
COFFEE_MACHINE, CABINET = 165, [[136], [146]]
SCREEN = 149
BALLOONS = 107
BOOKSHELF, CUPBOARD = 241, 233
PLANT_TREE, PLANT_TALL, PLANT_FERN, FLOWERS, FLOWERS_YELLOW = 103, 104, 83, 105, 106
PLANT_PALM, PLANT_SPIKY, PLANT_GRASS, PLANT_BUSH = 86, 89, 88, 90
PAINTINGS = [15, 16, 39, 18, 17]
PAINTING_WIDE, WORLD_MAP = 13, 37
PAINT_SUNFLOWERS, PAINT_BIRD, PAINT_CITY, PAINT_CHERRY, PAINT_GARDEN, PAINT_FLOWER, PAINT_LEMON = 15, 16, 39, 17, 18, 40, 41
BLANK_BOARD = [[63, 64]]
RUG = 733                                  # light grey carpet (WA_Room_Builder), laid over the room floor

# ── Garden (WA_Exterior) ──
TREES = ([[[1833 + c, 1834 + c], [1858 + c, 1859 + c], [1883 + c, 1884 + c]] for c in (0, 3, 6, 9, 12)]
         + [2133, 2136, 2139])                                   # 2x3: round trees and pines
BIG_TREES = ([[[1908 + c + i + r * 25 for i in range(3)] for r in range(3)] for c in (0, 3, 6, 9, 12)]
             + [2209, 2212, 2215])                               # 3x3
BLOSSOM_TREES = [TREES[3], TREES[4], BIG_TREES[3], BIG_TREES[4]]
SMALL_TREES = [2168, 2170]                                       # 2x2
BUSHES = [2284, 2288, 2286]
ROCKS = [2309, 2312, 2313, 2311]
TUFTS = [2339, 2340, 2341, 2342]
FLOWER_PATCHES = [2359, 2362, 2365, 2367, 2360, 2390, 2392]
FLOWERS_SMALL = [2409, 2410, 2411, 2412, 2414, 2415, 2416, 2417, 2434, 2435, 2436, 2438, 2439, 2440, 2441]
LILY_PADS, LOTUS, DUCK = [2464, 2465, 2489, 2490, 2491, 2516], 2468, 2492
FOUNTAIN = [[2521, 2522], [2546, 2547]]
PILLAR = 2513
BENCH, BENCH_L, BENCH_R = 1518, 1521, 1522
BARRELS = 2634


def layout(m):
    import random
    rng = random.Random(7)
    for name, ((x1, y1, x2, y2), floor) in ROOMS.items():
        m.fill("floor1", x1, y1, x2, y2, floor)
    for x1, y1, x2, y2 in DOORS:
        m.fill("floor1", x1, y1, x2, y2, WOOD)
    m.build_walls()
    tree = lambda g, x, y: m.put(g, x, y, collide="base", above_rows=len(g if isinstance(g, list) else m.obj(g)) - 1)
    plant = lambda g, x, y: m.put(g, x, y, collide="base", above_rows=len(g if isinstance(g, list) else m.obj(g)) - 1)
    wall = lambda g, x, y: m.put(g, x, y, layer="walls2", collide="none")
    deco = lambda g, x, y: m.put(g, x, y, layer="furniture2", collide="none")   # on top of tables / floor

    # Meeting rooms: glass table on the left, whiteboard, each one dressed differently.
    for x0 in MEETING_ROOMS:
        m.stamp(*MEETING_TABLE, x0 + 1, 4)
        m.stamp(*WHITEBOARD, x0 + 5, 1)
    x1, x2, x3 = MEETING_ROOMS
    # 1: navy, sunflowers and a palm
    m.stamp(*WINDOW, x1, 1); wall(PAINT_SUNFLOWERS, x1 + 3, 1); wall(PAINT_LEMON, x1 + 8, 1)
    plant(PLANT_PALM, x1 + 6, 4); plant(PLANT_FERN, x1, 10)
    # 2: slate, bird painting, tall plants
    m.stamp(*WINDOW, x2, 1); wall(PAINT_BIRD, x2 + 3, 1); wall(PAINT_CHERRY, x2 + 8, 1)
    plant(PLANT_SPIKY, x2 + 8, 4); plant(PLANT_TALL, x2, 10); plant(PLANT_TREE, x2 + 6, 5)
    # 3: honey wood, bookshelf
    m.stamp(*WINDOW, x3, 1); m.put(BOOKSHELF, x3 + 2, 1); wall(PAINT_CITY, x3 + 8, 1)
    plant(PLANT_PALM, x3 + 6, 4); plant(PLANT_TREE, x3, 10)

    # Team desks (top right): one long desk (quiet zone) and a reading corner.
    m.stamp(*LONG_DESK, 32, 3)
    m.stamp(*WINDOW, 31, 1); m.stamp(*WINDOW, 33, 1)
    wall(PAINTING_WIDE, 35, 1); m.stamp(*CLOCK, 37, 1)
    m.put(BOOKSHELF, 38, 1); m.put(BOOKSHELF, 40, 1)
    plant(PLANT_SPIKY, 42, 3); plant(PLANT_TALL, 31, 10)
    m.fill("floor1", 38, 5, 42, 10, RUG)
    m.put(SOFA_BLUE, 39, 5)
    m.put(TABLE_ROUND_WHITE, 40, 8); deco(FLOWERS, 40, 8)
    m.put(ARMCHAIR_BEIGE, 38, 8); m.put(ARMCHAIR_BEIGE, 41, 8)

    # Open office: two pods each side, a pool corner and a lounge in the middle.
    for x, y in PODS:
        m.stamp(*POD, x, y)
    m.stamp(*PRINTER, 1, 15); m.stamp(*BIN, 1, 17)
    m.stamp(*PRINTER, 42, 15); m.stamp(*BIN, 42, 17)
    wall(PAINT_GARDEN, 3, 13); m.put(BOOKSHELF, 10, 13); wall(PAINTING_WIDE, 13, 13)
    wall(PAINT_CHERRY, 21, 13); wall(WORLD_MAP, 24, 13); wall(PAINT_BIRD, 31, 13); wall(PAINT_FLOWER, 35, 13)
    plant(PLANT_TREE, 1, 21); plant(PLANT_SPIKY, 42, 21); plant(PLANT_TALL, 32, 22)
    px, py = POOL_TABLE
    m.fill("floor1", px - 3, py - 2, px + POOL_COLS + 2, py + POOL_ROWS + 1, RUG)
    m.put(pool_grid(m.pool_firstgid()), px, py, collide="all")
    for x, y in ((px - 2, py - 1), (px + POOL_COLS + 1, py - 1), (px - 2, py + POOL_ROWS), (px + POOL_COLS + 1, py + POOL_ROWS)):
        m.put(STOOL_WOOD, x, y, collide="none")
    plant(PLANT_PALM, 21, 14)
    m.fill("floor1", 24, 16, 31, 22, RUG)
    m.put(SOFA_BEIGE, 25, 16)
    m.put(TABLE_BIG, 25, 19); deco(FLOWERS_YELLOW, 26, 20)
    m.put(POUF_ORANGE, 29, 18); m.put(POUF_ORANGE, 29, 20)
    plant(PLANT_BUSH, 31, 16)

    # Coffee corner: kitchen, coffee machine, café tables with flowers.
    m.stamp(*KITCHEN, 1, 27)
    m.put(COUNTER, 3, 27)
    m.put(COFFEE_MACHINE, 6, 27, collide="all")
    m.put(CABINET, 7, 27, collide="all")
    for i, (x, y) in enumerate(((5, 31), (9, 30), (12, 28))):
        m.put(TABLE_ROUND_ORANGE, x, y)
        deco((FLOWERS, FLOWERS_YELLOW)[i % 2], x, y)
        m.put(STOOL_WOOD, x - 1, y, collide="none")
        m.put(STOOL_WOOD, x + 1, y, collide="none")
    wall(PAINT_LEMON, 3, 25); wall(PAINTING_WIDE, 9, 25); wall(PAINT_CHERRY, 12, 25)
    plant(PLANT_SPIKY, 9, 27); plant(PLANT_TALL, 13, 31)
    m.stamp(*BIN, 1, 32)

    # Reception: logo, desk, the all-hands stage, sofas and plants.
    m.stamp(*LOGO, 19, 25)
    m.put(COUNTER, 20, 27); deco(FLOWERS, 22, 27)
    wall(PAINT_SUNFLOWERS, 16, 25); wall(PAINT_BIRD, 28, 25)
    m.put(SOFA_BEIGE, 15, 27)
    m.stamp(*ARMCHAIRS, 28, 27)
    m.put(SOFA_BLUE, 25, 31)
    plant(PLANT_PALM, 15, 30)
    m.put(FLOWERS_YELLOW, 28, 32, collide="all")
    m.fill("start", 20, 31, 22, 32, START)

    # Games area: screen with poufs and sofa, board-game table, game tables, balloons.
    wall(SCREEN, 35, 25); wall(WORLD_MAP, 40, 25); wall(PAINT_CITY, 31, 25)
    for x in (35, 37):
        m.put(POUF_ORANGE, x, 27, collide="none")
    m.put(SOFA_YELLOW, 35, 30)
    m.put(TABLE_BIG, 31, 30)
    m.put(STOOLS[0], 30, 31, collide="none"); m.put(STOOLS[1], 34, 31, collide="none")
    for i, (x, y) in enumerate(GAME_TABLES):
        m.put([[m.game_firstgid() + i]], x, y)                            # a table showing its game (GAMES[i])
        m.put(STOOLS[i % 4], x - 1, y, collide="none")
        m.put(STOOLS[(i + 1) % 4], x + 1, y, collide="none")
    m.put(BALLOONS, 42, 25, collide="none"); m.put(BALLOONS, 30, 25, collide="none")
    plant(PLANT_TALL, 42, 31)

    garden(m, rng, tree)


def garden(m, rng, tree):
    (gx1, gy1, gx2, gy2), _ = ROOMS["Garden"]
    used = set()
    def mark(x, y, w, h):
        used.update((x + dx, y + dy) for dx in range(w) for dy in range(h))
    def free(x, y, w, h):
        return all(gx1 <= x + dx <= gx2 and gy1 <= y + dy <= gy2 and (x + dx, y + dy) not in used
                   for dx in range(w) for dy in range(h))
    def place(g, x, y, **kw):
        w, h = m.put(g, x, y, **kw); mark(x, y, w, h)
    def plant_tree(g, x, y):
        grid = g if isinstance(g, list) else m.obj(g)
        tree(g, x, y); mark(x, y, len(grid[0]), len(grid))

    for y in range(gy1, gy2 + 1):              # lawn with a little texture
        for x in range(gx1, gx2 + 1):
            m.set("floor1", x, y, rng.choice(GRASS_VARIANTS))

    # Paths: from each garden door to a main path along the building.
    def path(x1, y1, x2, y2):
        m.fill("floor1", x1, y1, x2, y2, COBBLE); mark(x1, y1, x2 - x1 + 1, y2 - y1 + 1)
    for dx, y1, _, y2 in GARDEN_DOORS:
        path(dx, y1, 46, y2)
    path(46, GARDEN_DOORS[0][1], 47, GARDEN_DOORS[-1][3])

    # Picnic lawn (north): a big wooden table with stools, a blossom tree for shade.
    m.put(TABLE_BIG, 50, 3); mark(49, 3, 5, 4)
    for sx, sy in ((49, 4), (53, 4), (51, 6)):
        m.put(STOOL_WOOD, sx, sy, collide="none")
    plant_tree(BLOSSOM_TREES[2], 54, 3)

    # Flower meadow with benches around it.
    for y in range(9, 14):
        for x in range(49, 57):
            if rng.random() < 0.45:
                m.put(rng.choice(FLOWER_PATCHES + FLOWERS_SMALL), x, y, layer="furniture2", collide="none")
    mark(49, 9, 8, 5)
    for x in (50, 54):
        m.put(BENCH, x, 8); mark(x, 8, 2, 1)
    m.put(BENCH_R, 48, 10); m.put(BENCH_R, 48, 12); mark(48, 10, 1, 4)

    # Duck pond, rounded, with lily pads, lotus flowers and ducks; benches on the north shore.
    px1, py1, px2, py2 = 50, 16, 56, 21
    pond = {(x, y) for x in range(px1, px2 + 1) for y in range(py1, py2 + 1)}
    for cx, cy in ((px1, py1), (px2, py1), (px1, py2), (px2, py2)):
        pond -= {(cx, cy), (cx + (1 if cx == px1 else -1), cy), (cx, cy + (1 if cy == py1 else -1))}
    for x, y in pond:
        m.set("floor1", x, y, WATER); m.set("collisions", x, y, COLLIDE)
    mark(px1 - 1, py1 - 1, px2 - px1 + 3, py2 - py1 + 3)
    for x, y, g in ((51, 17, LILY_PADS[0]), (52, 17, LILY_PADS[2]), (55, 17, LILY_PADS[1]), (53, 19, LILY_PADS[3]),
                    (51, 20, LILY_PADS[4]), (54, 20, LILY_PADS[5]), (54, 17, LOTUS), (51, 19, LOTUS),
                    (52, 18, DUCK), (54, 19, DUCK)):
        m.put(g, x, y, layer="furniture2", collide="none")
    for x, y, g in ((49, 18, ROCKS[1]), (57, 19, ROCKS[2]), (53, 22, ROCKS[1])):
        m.put(g, x, y, collide="all")
    for x in (50, 54):
        m.put(BENCH, x, 15)

    # Meditation garden (south): hedged stone patio, lotus pool, cushions, pillars and rocks. A silent zone.
    mx1, my1, mx2, my2 = MEDITATION
    mark(mx1, my1, mx2 - mx1 + 1, my2 - my1 + 1)
    for x in range(mx1, mx2 + 1, 2):                                    # hedge along the top
        m.put(BUSHES[0] if x + 1 <= mx2 else [[BUSHES[0]]], x, my1, collide="all")
    m.fill("floor1", mx1, my1 + 1, mx2, my2, COBBLE)
    cx = (mx1 + mx2) // 2                                               # pool centre column
    for x in range(cx - 1, cx + 2):
        for y in (my1 + 2, my1 + 3):
            m.set("floor1", x, y, WATER); m.set("collisions", x, y, COLLIDE)
    m.put(LOTUS, cx - 1, my1 + 2, layer="furniture2", collide="none")
    m.put(LILY_PADS[2], cx + 1, my1 + 3, layer="furniture2", collide="none")
    for x, y, c in ((cx - 2, my1 + 2, 2), (cx - 2, my1 + 3, 0), (cx + 2, my1 + 2, 0), (cx + 2, my1 + 3, 2),
                    (cx, my1 + 1, 1), (cx, my2, 3)):
        m.put(STOOLS[c], x, y, collide="none")                          # meditation cushions
    for x in (mx1 + 1, mx2 - 1):
        m.put(PILLAR, x, my1 + 1, layer="furniture2", collide="base", above_rows=1)
    m.put(ROCKS[3], mx1, my2 - 1, collide="all"); m.put(ROCKS[0], mx2 - 1, my2, collide="all")
    for x, y in ((cx - 1, my1 + 1), (cx + 1, my1 + 1), (cx - 1, my2), (cx + 1, my2)):
        m.put(TUFTS[0], x, y, layer="furniture2", collide="none")

    # Barrels and hedges along the building.
    place(BARRELS, 44, 31, collide="all"); place(BARRELS, 44, 9, collide="all")
    for y in (1, 3, 12, 14, 22, 25):
        if free(44, y, 2, 1): place(rng.choice(BUSHES[:2]), 44, y, collide="all")

    # Trees framing the garden: top, right and bottom.
    for x in (48, 51, 54):
        if free(x, 0, 3, 3): plant_tree(rng.choice(BIG_TREES), x, 0)
    for y in range(0, gy2 + 1, 3):
        yy = min(y, gy2 - 2)
        if free(57, yy, 3, 3): plant_tree(rng.choice(BIG_TREES), 57, yy)
    for x in (47, 50, 53):
        if free(x, gy2 - 2, 3, 3): plant_tree(rng.choice(BIG_TREES), x, gy2 - 2)

    # Sprinkle tufts and wild flowers on the remaining lawn.
    for y in range(gy1, gy2 + 1):
        for x in range(gx1, gx2 + 1):
            if (x, y) in used: continue
            r = rng.random()
            if r < 0.07: m.put(rng.choice(FLOWERS_SMALL), x, y, layer="furniture2", collide="none")
            elif r < 0.10: m.put(rng.choice(TUFTS), x, y, layer="furniture2", collide="none")


# Free browser games (open in a new tab; no accounts needed to play with colleagues).
GAME_TABLES = [(32, 28), (40, 28), (40, 31)]
POOL_TABLE = (15, 18)                      # top-left of the 4x3 pool table
PODS = [(2, 16), (6, 16), (34, 16), (38, 16)]   # top-left of each 4-desk pod
POD_SEATS = [(0, 1), (3, 1), (0, 3), (3, 3)]   # chair offsets inside a pod
MEDITATION = (48, 24, 56, 28)              # silent meditation garden, south of the pond (top row is the hedge)
STAGE = (18, 29, 25, 30)                   # megaphone speaker zone in reception, in front of the desk
FOCUS_DESKS = (31, 3, 37, 11)              # silent zone around the team long desk
BIRDSONG = "https://upload.wikimedia.org/wikipedia/commons/transcoded/8/80/Birds_singing_in_garden.ogg/Birds_singing_in_garden.ogg.mp3"
POOL_GAME = ("8-ball pool (Foony)", "https://foony.com/games/8-ball-pool-online-billiards")
GAMES = [
    ("Chess", "https://lichess.org/"),
    ("Pictionary (skribbl.io)", "https://skribbl.io/"),
    ("Codenames", "https://codenames.game/"),
]


def wam():
    def area_id(name): return str(uuid.uuid5(uuid.NAMESPACE_URL, "office/" + name))
    def area(name, x1, y1, x2, y2, props):
        return {"id": area_id(name), "name": name, "visible": True,
                "x": x1 * 32, "y": y1 * 32, "width": (x2 - x1 + 1) * 32, "height": (y2 - y1 + 1) * 32,
                "properties": props}
    def pid(*parts): return str(uuid.uuid5(uuid.NAMESPACE_URL, "office/" + "/".join(parts)))
    def listen(name):
        return {"id": pid(name, "listener"), "type": "listenerMegaphone", "speakerZoneName": area_id("Stage"),  # speaker area id
                "chatEnabled": True}
    def describe(name, text):
        return {"id": pid(name, "description"), "type": "areaDescriptionProperties", "description": text, "searchable": True}
    areas = []
    for i, x0 in enumerate(MEETING_ROOMS, start=1):
        name = f"Meeting room {i}"
        areas.append(area(name, x0, 3, x0 + 8, 11, [
            describe(name, "Everyone inside joins one video call (LiveKit)."),
            {"id": pid(name, "livekit"), "type": "livekitRoomProperty", "roomName": f"meeting-room-{i}",
             "livekitRoomConfig": {"startWithAudioMuted": False, "startWithVideoMuted": False}},
            {"id": pid(name, "focus"), "type": "focusable", "zoom_margin": 0.5},
            {"id": pid(name, "max"), "type": "maxUsersInAreaPropertyData", "maxUsers": 8},
        ]))
        # Entry point for links straight into the room: .../office.wam#meeting-room-1 (WA doesn't decode spaces).
        areas.append(area(f"meeting-room-{i}", x0 + 6, 9, x0 + 7, 10, [
            {"id": pid(name, "start"), "type": "start", "isDefault": False}]))
    for name, text in (("Reception", "Welcome! Walk up to people to talk."),
                       ("Coffee corner", "Grab a coffee and chat."),
                       ("Games area", "Walk to a table and press SPACE to start a game."),
                       ("Open office", "Desks for everyone, a lounge corner and a pool table."),
                       ("Team desks", "Team tables (quiet zone) and a reading corner."),
                       ("Garden", "Fresh air and birdsong: picnic tables, a flower meadow, the duck pond and a meditation garden.")):
        (x1, y1, x2, y2), _ = ROOMS[name]
        top = y1 + 2 if name not in ("Open office", "Garden") else y1   # skip the wall face rows
        # No megaphone listening here: a listener area shows "waiting for a speaker" and replaces
        # proximity bubbles, so only the small audience area in front of the stage listens.
        props = [describe(name, text)]
        if name == "Garden":
            props.append({"id": pid(name, "audio"), "type": "playAudio", "audioLink": BIRDSONG, "volume": 0.3})
        areas.append(area(name, x1, top, x2, y2, props))

    # All-hands megaphone: speak from the stage, everyone in the reception audience hears you.
    areas.append(area("Stage", *STAGE, [
        describe("Stage", "All-hands stage: speak here and everyone in the reception audience hears you."),
        {"id": pid("Stage", "speaker"), "type": "speakerMegaphone", "name": "All-hands", "chatEnabled": True,
         "seeAttendees": True},
        {"id": pid("Stage", "highlight"), "type": "highlight", "opacity": 0.4, "color": "#f5c542"},
    ]))
    (rx1, _, rx2, ry2), _ = ROOMS["Reception"]
    areas.append(area("Reception audience", rx1, STAGE[3] + 1, rx2, ry2, [listen("Reception audience")]))

    # Focus desks: nobody can start a conversation with you here.
    areas.append(area("Focus desks", *FOCUS_DESKS, [
        describe("Focus desks", "Quiet zone: no conversations, for heads-down work."),
        {"id": pid("Focus desks", "silent"), "type": "silent"},
    ]))

    mx1, my1, mx2, my2 = MEDITATION
    areas.append(area("Meditation garden", mx1, my1 + 1, mx2, my2, [
        describe("Meditation garden", "Silent zone: sit by the lotus pool and breathe. No conversations here."),
        {"id": pid("Meditation garden", "silent"), "type": "silent"},
    ]))

    # Personal desks: each pod seat can be claimed by one person.
    for p, (px, py) in enumerate(PODS, start=1):
        for s, (dx, dy) in enumerate(POD_SEATS, start=1):
            name = f"Desk {p}.{s}"
            areas.append(area(name, px + dx, py + dy, px + dx, py + dy + 1, [
                {"id": pid(name, "personal"), "type": "personalAreaPropertyData", "accessClaimMode": "dynamic",
                 "allowedTags": [], "ownerId": None},
            ]))

    # Pool table.
    game, link = POOL_GAME
    px, py = POOL_TABLE
    areas.append(area("Game: Pool", px - 3, py - 2, px + POOL_COLS + 2, py + POOL_ROWS + 1, [
        {"id": pid("pool", "site"), "type": "openWebsite", "link": link, "newTab": True, "trigger": "onaction",
         "triggerMessage": f"Press SPACE to play {game}", "closable": True, "application": "website"},
    ]))
    for (x, y), (game, link) in zip(GAME_TABLES, GAMES):
        areas.append(area(f"Game: {game}", x - 1, y - 1, x + 1, y + 1, [
            {"id": pid(game, "site"), "type": "openWebsite", "link": link, "newTab": True, "trigger": "onaction",
             "triggerMessage": f"Press SPACE to play {game}", "closable": True, "application": "website"},
        ]))
    return {
        "version": "2.1.0", "mapUrl": "./office.tmj", "entities": {}, "areas": areas,
        "entityCollections": [{"url": "https://__DOMAIN__/collections/FurnitureCollection.json", "type": "file"},
                              {"url": "https://__DOMAIN__/collections/OfficeCollection.json", "type": "file"}],
        "metadata": {"name": "Office", "description": "Reception, coffee corner, games area, open office, three meeting rooms and a garden.",
                     "copyright": "Tiles: WorkAdventure map-starter-kit, CC-BY-SA 3.0"},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--starter-kit", help="path to a map-starter-kit checkout (cloned if omitted)")
    ap.add_argument("--preview", help="also render a PNG preview (needs Pillow)")
    a = ap.parse_args()
    starter = a.starter_kit
    if not starter:
        starter = os.path.join(tempfile.gettempdir(), f"map-starter-kit-{STARTER_KIT_TAG}")
        if not os.path.isdir(starter):
            subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", STARTER_KIT_TAG, STARTER_KIT_REPO, starter], check=True)
    os.makedirs(os.path.join(HERE, "tilesets"), exist_ok=True)
    draw_pool_table(os.path.join(HERE, POOL_TILESET))    # lives next to the map; upload-map.sh adds it to the kit
    draw_game_tables(os.path.join(HERE, GAME_TILESET))
    m = Map(starter)
    layout(m)
    with open(os.path.join(HERE, "office.tmj"), "w") as f: json.dump(m.tmj(), f)
    with open(os.path.join(HERE, "office.wam.template"), "w") as f: json.dump(wam(), f, indent=2)
    print(f"wrote office.tmj ({W}x{H}) and office.wam.template")
    if a.preview:
        sys.path.insert(0, HERE)
        from render import render
        render(os.path.join(HERE, "office.tmj"), a.preview, tileset_dir=starter,
               areas=os.path.join(HERE, "office.wam.template"))
        print("wrote", a.preview)


if __name__ == "__main__":
    main()
