#!/usr/bin/env python3
"""
build_from_spec.py — baca spec.json hasil export dari island_editor.html,
bikin terrain zip-nya, lalu daftarin ke region_templates.json +
archipelago_templates.json (pakai register_templates() dari gen_island.py).

Pipeline lengkap:
  1. Buka island_editor.html (Artifact) di browser (HP/PC sama-sama bisa)
  2. Lukis biome, taruh natural/landmark/dermaga/herd
  3. Export spec.json
  4. Commit spec.json ke folder island_specs/ di repo kamu (atau upload manual)
  5. Jalanin: python3 tools/build_from_spec.py island_specs/xxx.spec.json \\
       --terrains-dir data/terrains \\
       --region-templates data/assets/region_templates.json \\
       --archipelago-templates data/assets/archipelago_templates.json \\
       [--archipelago-key 40TuT01 ...]
  6. Timpa hasil (.zip + 2 json) ke data server offline kamu, restart.

Format herds.yml/pois.yml/whole.landmarks/whole.garden mengikuti spec yang sama
dengan gen_island.py (lihat docstring di sana buat detail byte-level).
"""
import argparse
import base64
import json
import random
import re
from collections import deque
import struct
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_island import BIOME  # reuse
from safe_registry import add_region_template, alias_archipelago, OWN_MARKERS
from water_layers import build_water_layers as derive_water_layers, decode_spec_water

PRESETS = json.loads((Path(__file__).parent / "island_presets.json").read_text(encoding="utf-8"))
ISLAND_TYPES = PRESETS["types"]          # tipe pulau -> ocean/lake/river biome, kode tile, dst (dari data asli)


def build_landmarks_bytes(landmarks):
    buf = bytearray()
    for l in landmarks:
        buf += struct.pack(
            "<HHHBhhhBBB",
            l["x"], l["y"], l.get("id", 1), l.get("rotate", 0),
            l.get("offsetX", 0), l.get("offsetY", 0), l.get("offsetZ", 0),
            l.get("scaleX", 16), l.get("scaleY", 16), l.get("scaleZ", 16),
        )
    return bytes(buf)


# Thornbush = PROP penghalang (id 410, ClientRemovableProp) di tutorial asli, BUKAN natural.
# Natural 11001/11039 adalah bush_blackthorn (tanaman biasa yang bisa dipanen). Data resource: natural.txt.
THORN_PROP_ID = "410"
THORN_PLANT_IDS = (11001, 11039)
OLD_TRIGGER_ALIAS = {"trigger_arrive_thornbush": "trigger_arrive_obstacle"}  # nama alur lama tidak ada di alur tutorial asli


def normalize_thorns(raw):
    """[{x,y,...}] -> [{x,y,prop_id}] unik per tile."""
    seen, out = set(), []
    for t in raw or []:
        if isinstance(t.get("x"), int) and isinstance(t.get("y"), int) and (t["x"], t["y"]) not in seen:
            seen.add((t["x"], t["y"]))
            out.append({"x": t["x"], "y": t["y"], "prop_id": THORN_PROP_ID})
    return out


def normalize_triggers(raw):
    out = []
    for t in raw or []:
        t = dict(t)
        t["flow"] = OLD_TRIGGER_ALIAS.get(t.get("flow"), t.get("flow"))
        out.append(t)
    return out


def build_garden_bytes(naturals):
    buf = bytearray()
    for n in naturals:
        buf += struct.pack("<HHH", n["x"], n["y"], n["entityType"])
    return bytes(buf)


def build_camp_artifacts_lines(buildings):
    """Bangunan kamp dari editor (safe house/warp hole kamp, pusat komunikasi, pick-up point, dll)."""
    if not buildings:
        return ["camp_artifacts: []"]
    lines = ["camp_artifacts:"]
    for b in buildings:
        lines.append(f"- entity_type: {b['entityType']}")
        lines.append(f"  tile: [{b['x']}, {b['y']}]")
    return lines


def build_pois_yml(port_points, buildings=None, warpholes=None, rifts=None):
    lines = ["port_points:"]
    if port_points:
        for p in port_points:
            lines.append(f"- [{p['x']}, {p['y']}]")
    else:
        lines[0] = "port_points: []"
    # format sama persis dengan pois.yml pulau asli (server: Support_TerrainPois.Parse)
    if warpholes:
        lines.append("warpholes:")
        for i, (x, y) in enumerate(warpholes):
            lines += [f"  {i}:", f"  - {x}", f"  - {y}"]
    else:
        lines.append("warpholes: {}")
    if rifts:
        lines.append("rifts:")
        for x, y in rifts:
            lines += [f"- - {x}", f"  - {y}"]
    else:
        lines.append("rifts: []")
    lines.append("craters: []")
    lines += build_camp_artifacts_lines(buildings)
    return "\n".join(lines) + "\n"


def build_herds_yml(herds):
    if not herds:
        return None
    by_group = {}
    for h in herds:
        by_group.setdefault(h["group"], []).append(h)
    lines = ["herds:"]
    counter = 300
    for group, points in by_group.items():
        lines.append(f"  {group}:")
        for p in points:
            counter += 1
            lines.append(f"  - id: {counter}")
            lines.append(f"    tile: [{p['x']}, {p['y']}]")
            # NB: entityType di titik ini disimpan cuma buat referensi kamu sendiri;
            # spesies aktual yang di-spawn server ditentukan dari region_templates.json
            # -> herds -> <group> -> spawns (urutan index), BUKAN dibaca dari herds.yml.
            # Kalau entityType di editor beda2 per titik dalam 1 grup, cocokkan manual
            # urutan 'spawns' di region_templates.json biar sesuai.
    return "\n".join(lines) + "\n"


def default_level(entity_type):
    """Level tempur default hewan: modus dari semua template asli (fallback 20)."""
    from collections import Counter
    c = Counter()
    for groups in PRESETS["species"].values():
        for rows in groups.values():
            for base, lv, n in rows:
                if base == entity_type:
                    c[lv] += n
    return c.most_common(1)[0][0] if c else 20


def pack_spawn(h):
    """Server membaca spawns sebagai angka 6 digit: <jenis hewan 4 digit><level tempur 2 digit>
    (Support_RegionCatalog.cs HerdSpawn.FromPacked = packed/100, packed%100). Versi lama menulis
    id 4 digit (mis. 2001) => server membacanya sebagai jenis '20' yang tidak ada => hewan tidak muncul."""
    et = int(h["entityType"])
    if et >= 10000:                       # sudah packed
        return et
    lv = int(h.get("level") or default_level(et))
    info = PRESETS["animals"].get(str(et))
    if info:
        lv = max(info[2], min(info[3], lv))
    return et * 100 + max(1, min(99, lv))


def herds_to_spawns(herds):
    """1 titik herd di editor = 1 hewan. Urutan spawns per grup = urutan titik di herds.yml
    (id 301, 302, ... per grup); server memasangkan spawns[i] dengan titik ke-i di grupnya."""
    by_group = {}
    for h in herds:
        if h.get("entityType"):
            by_group.setdefault(h["group"], []).append(pack_spawn(h))
    return {g: {"total_count": len(sp), "spawns": sp} for g, sp in by_group.items()}


def resolve_island_type(spec):
    t = spec.get("island_type")
    return t if t in ISLAND_TYPES else dominant_biome_name(spec)


def pick_like_template(regs, itype, level):
    """Template ASLI (bukan buatan tool) bertipe sama dengan level terdekat — jadi kerangka entry baru
    supaya semua field yang dibaca server/client lengkap (bukan entry kosong)."""
    best = None
    for tid, t in regs.items():
        if not isinstance(t, dict) or any(m in t for m in OWN_MARKERS):
            continue
        be = [b for b in (t.get("biome_effects") or {}) if b in ISLAND_TYPES]
        if not be or be[0] != itype or not t.get("herds"):
            continue
        d = abs(int(t.get("level", 0)) - level)
        if best is None or d < best[0]:
            best = (d, tid)
    return best[1] if best else None


def warn_species_mismatch(spec, itype):
    allowed = {}
    for g, rows in PRESETS["species"].get(itype, {}).items():
        allowed[g] = {r[0] for r in rows}
    bad = 0
    for h in spec.get("herds", []):
        et = int(h.get("entityType") or 0)
        et = et // 100 if et >= 10000 else et
        if allowed.get(h["group"]) and et not in allowed[h["group"]]:
            bad += 1
    if bad:
        print(f"[build_from_spec] PERINGATAN: {bad} titik herd berisi hewan yang tidak dipakai template asli "
              f"tipe '{itype}' (grup yang sama). Tetap ditulis.")


def build_region_entry(regs, spec, itype, like_id):
    like = regs.get(like_id) if like_id else None
    if like_id and like is None:
        raise SystemExit(f"--like-template '{like_id}' tidak ada di region_templates.json")
    entry = json.loads(json.dumps(like)) if like else {}
    entry["level"] = spec["level"]
    entry["role"] = spec.get("role", 4)
    be = entry.get("biome_effects") or {}
    if not be or next(iter(be)) != itype:
        first = next(iter(be.values()), {}) if be else {}
        entry["biome_effects"] = {itype: first}
    new_herds = herds_to_spawns(spec.get("herds", []))
    if new_herds:
        entry["herds"] = new_herds      # hanya grup yang punya titik; sisanya tidak dipakai server
    entry["__added_by_gen_island__"] = True
    return entry


LAND_CODES = set(range(0, 8))          # 0-7 = biome darat
OCEAN_CODES, RIVER_CODES, LAKE_CODES = {11, 12}, {13}, {14}
THEME_BY_BIOME = {"temperate_forest": "temperate", "tropical_forest": "tropical", "desert": "desert",
                  "tundra": "tundra", "snow_field": "snow", "grassland": "grassland",
                  "swamp_mud": "swamp", "volcanic": "volcanic"}


def guess_theme(biome_name, like_template=None):
    """Tema tile_set/color_set. Template contoh 'sa'/'sv' (savanna) menang atas biome grassland."""
    if like_template and re.search(r"\d+(sa|sv)", like_template):
        return "savanna"
    return THEME_BY_BIOME.get(biome_name, "grassland")


def build_water_layers(spec, biomes, W, H):
    """whole.ocean + whole.rivers.
    1) PAKAI lapisan yang sudah dihitung editor (spec.ocean_b64 / rivers_b64) — itu persis yang terlihat di pratinjau editor.
    2) Kalau spec lama tidak punya (atau ukurannya salah), hitung dengan port algoritma editor (water_layers.py).
    Dulu builder ini selalu menghitung sendiri versi sederhana: arus (127,127) = tanpa arus dan kedalaman sungai
    tetap 180, sehingga sungai tampak diam dan warnanya tidak menyatu dengan danau/laut."""
    layers = decode_spec_water(spec, W, H)
    if layers:
        print("[build_from_spec] lapisan air: dari editor (ocean_b64/rivers_b64)")
        return layers
    print("[build_from_spec] lapisan air: dihitung ulang (spec tidak membawa ocean_b64/rivers_b64)")
    return derive_water_layers(biomes, W, H)


def auto_poi_points(spec, biomes):
    """Warp hole (5) + rift (2) otomatis — pulau asli punya 5 & 2 (config.yml: ExtraWarpholes 5,
    RiftCount 2, jarak min 50; jauh dari dermaga 40). Editor belum punya alat untuk ini, dan tanpa
    ini pulau tidak punya warp hole sama sekali. Deterministik: seed = template_id."""
    W, H = spec["width"], spec["height"]
    rng = random.Random(spec["template_id"])
    ports = [(p["x"], p["y"]) for p in spec.get("port_points", [])]
    blocked = [(b["x"], b["y"]) for b in spec.get("buildings", [])]

    def ok_land(x, y, r=5):  # jendela (2r+1)^2 harus darat penuh dan tidak collidable
        if x - r < 0 or y - r < 0 or x + r >= W or y + r >= H:
            return False
        for yy in range(y - r, y + r + 1):
            for xx in range(x - r, x + r + 1):
                b = biomes[yy * W + xx]
                if (b & 0x3F) not in LAND_CODES or b & 0x80:
                    return False
        return True

    cands = [(x, y) for y in range(5, H - 5, 3) for x in range(5, W - 5, 3) if ok_land(x, y)]
    rng.shuffle(cands)

    def pick(n, placed, min_d):
        out = []
        for d in (min_d, min_d * 0.6, min_d * 0.35, 8):   # longgarkan jarak kalau pulau kecil
            for x, y in cands:
                if len(out) >= n:
                    break
                if any((x - a) ** 2 + (y - b) ** 2 < d * d for a, b in placed + out) or \
                   any((x - a) ** 2 + (y - b) ** 2 < 25 * 25 for a, b in ports + blocked):
                    continue
                out.append((x, y))
        return out

    warpholes = pick(5, [], 50)
    rifts = pick(2, warpholes, 50)
    return warpholes, rifts


def dominant_biome_name(spec):
    """Tebak biome dominan pulau dari tile yang paling sering muncul di grid —
    dipakai buat lake_biome/ocean_biome di info.yml dan biome_effects pas register."""
    from collections import Counter
    biomes = base64.b64decode(spec["biomes_b64"])
    # HANYA biome darat (0-7): laut (11/12) biasanya tile terbanyak, dan dulu dia yang menang =>
    # biome_effects jadi {"warm_ocean": ...} => MajorBiome salah/Invalid dan pulau hilang dari peta rute
    counts = Counter(b & 0x3F for b in biomes if (b & 0x3F) in LAND_CODES)
    if not counts:
        return "grassland"
    name_by_code = {v: k for k, v in BIOME.items()}
    return name_by_code.get(counts.most_common(1)[0][0], "grassland")


def build_info_yml(spec, like_template=None):
    itype = resolve_island_type(spec)
    pre = ISLAND_TYPES.get(itype)
    theme = guess_theme(itype, like_template)
    tile_set = {"snow": "snowfields"}.get(theme, theme)
    # prioritas entry_points: titik spawn editor > dermaga pertama > default (tengah-atas, biasanya LAUT)
    if spec.get("spawn_point"):
        entry = spec["spawn_point"]
    elif spec.get("port_points"):
        entry = spec["port_points"][0]
    else:
        entry = {"x": spec["width"] // 2, "y": 20}
    # id di whole.landmarks = index di list ini (editor bikin ulang list-nya tiap export)
    landmarks = [{"id": i, "prefab": p} for i, p in enumerate(spec.get("landmark_prefabs", []))]
    extra = {}
    # dibaca server (StarterIslands.ResolveLayout / NpcSpawner) dari info.yml — dulu TIDAK ditulis => diabaikan
    if spec.get("spawn_point"):
        extra["spawn_point"] = {"x": spec["spawn_point"]["x"], "y": spec["spawn_point"]["y"]}
    if spec.get("raft_point"):
        extra["raft_point"] = {"x": spec["raft_point"]["x"], "y": spec["raft_point"]["y"],
                               "size": spec["raft_point"].get("size", 4)}
    if spec.get("npcs"):
        extra["npcs"] = spec["npcs"]
    # Hewan objek (editor: "Hewan objek tak bisa diserang"). Dulu TIDAK ditulis => brachiosaurus tutorial tidak muncul.
    # Server tertanam (AnimalManager.SpawnStatic) membacanya dari info.yml -> static_animals.
    if spec.get("static_animals"):
        extra["static_animals"] = [
            {"x": a["x"], "y": a["y"], "entityType": a["entityType"], "yaw": a.get("yaw", 0),
             "attackable": bool(a.get("attackable", False)), "internal": a.get("internal", "")}
            for a in spec["static_animals"]
            if isinstance(a.get("x"), int) and isinstance(a.get("y"), int) and a.get("entityType")
        ]
    # zona pemicu misi tutorial dari island_editor (mode 🚩): client TerrainMeta.LoadTutorialTriggers -> PlayGuideSystem.TutorialZoneCheck.
    # Hanya data di info.yml; tidak masuk biome/tile sehingga tidak tampil di minimap game.
    if spec.get("tutorial_triggers"):
        extra["tutorial_triggers"] = normalize_triggers(spec["tutorial_triggers"])
    # Thornbush penghalang misi (editor: mode "Penghalang: Thornbush"): client TerrainMeta.LoadThornBushes -> ThornBushSystem.
    # Hanya data di info.yml (prop_id 410); BUKAN natural, jadi tidak ikut whole.garden dan tidak menjadi tanaman.
    thorns = normalize_thorns(spec.get("thorn_bushes"))
    if thorns:
        extra["thorn_bushes"] = thorns
    # Bangkai kereta / jalan raya rusak (ST_train_wreckage_01*, ST_highway_01*) = landmark GLOBAL: client memuatnya
    # dari info.yml -> global_landmarks (TerrainMeta.LoadGlobalLandmarks), BUKAN dari whole.landmarks.
    # Dulu builder mengabaikan spec.global_landmarks sehingga kereta rusak tidak pernah muncul.
    if spec.get("global_landmarks"):
        extra["global_landmarks"] = [
            {"x": l["x"], "y": l["y"], "id": l.get("id", 0), "rotate": l.get("rotate", 0) & 0xFF,
             "offsetX": l.get("offsetX", 0), "offsetY": l.get("offsetY", 0), "offsetZ": l.get("offsetZ", 0),
             "scaleX": l.get("scaleX", 16), "scaleY": l.get("scaleY", 16), "scaleZ": l.get("scaleZ", 16)}
            for l in spec["global_landmarks"]]
    return {
        **extra,
        "tile_count": [spec["width"], spec["height"]],
        "landmarks": landmarks,
        # nilai per tipe diambil dari info.yml pulau asli (mis. rawa: swamp_ocean/swamp_mud, vulkanik: lava)
        "lake_biome": pre["lake_biome"] if pre else "grassland",
        "ocean_biome": pre["ocean_biome"] if pre else "warm_ocean",
        "river_biome": pre["river_biome"] if pre else "temperate_forest",
        "color_set": tile_set,
        "region_template": spec["template_id"],
        "tile_set": tile_set,
        "entry_points": [[entry["x"], entry["y"]]],
    }


def build_zip(spec, out_path, like_template=None):
    biomes = base64.b64decode(spec["biomes_b64"])
    expect = spec["width"] * spec["height"]
    if len(biomes) != expect:
        raise SystemExit(f"biomes_b64 ukurannya {len(biomes)} byte, expect {expect}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        info = build_info_yml(spec, like_template)
        z.writestr("info.yml", json.dumps(info))
        z.writestr("config.yml", json.dumps({"theme": guess_theme(resolve_island_type(spec), like_template)}))
        z.writestr("whole.biomes", biomes)
        ocean, rivers = build_water_layers(spec, biomes, spec["width"], spec["height"])
        z.writestr("whole.ocean", ocean)
        z.writestr("whole.rivers", rivers)
        warpholes, rifts = auto_poi_points(spec, biomes)
        print(f"[build_from_spec] auto POI: {len(warpholes)} warp hole, {len(rifts)} rift")
        # spec lama: thornbush dulu disimpan sebagai natural 11001/11039 (tanaman blackthorn). Buang natural palsu di tile penghalang.
        _tk = {(t["x"], t["y"]) for t in normalize_thorns(spec.get("thorn_bushes"))}
        _nat = [n for n in spec.get("naturals", []) if not (n.get("entityType") in THORN_PLANT_IDS and (n.get("x"), n.get("y")) in _tk)]
        if len(_nat) != len(spec.get("naturals", [])):
            print(f"  [thorn] {len(spec.get('naturals', [])) - len(_nat)} natural blackthorn di tile penghalang dibuang (thornbush = prop, bukan tanaman)")
        garden = build_garden_bytes(_nat)
        if garden:
            z.writestr("whole.garden", garden)
        marks = build_landmarks_bytes(spec.get("landmarks", []))
        if marks:
            z.writestr("whole.landmarks", marks)
        z.writestr("pois.yml", build_pois_yml(spec.get("port_points", []), spec.get("buildings", []), warpholes, rifts))
        herds_yml = build_herds_yml(spec.get("herds", []))
        if herds_yml:
            z.writestr("herds.yml", herds_yml)
    print(f"[build_from_spec] zip ditulis: {out_path} ({out_path.stat().st_size} byte, "
          f"{len(spec.get('naturals', []))} natural, {len(spec.get('landmarks', []))} landmark, "
          f"{len(spec.get('global_landmarks', []))} landmark global, {len(spec.get('npcs', []))} npc, {len(spec.get('static_animals', []))} hewan-objek, "
          f"{len(spec.get('herds', []))} herd)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec_path", help="path *.spec.json hasil export island_editor.html")
    ap.add_argument("--terrains-dir", required=True)
    ap.add_argument("--region-templates", required=True)
    ap.add_argument("--archipelago-templates", required=True)
    ap.add_argument("--like-template", default=None,
                     help="id template real yang mau dicontek herds/biome_effects (opsional)")
    ap.add_argument("--archipelago-key", action="append", default=[])
    args = ap.parse_args()

    spec = json.loads(Path(args.spec_path).read_text())
    tid = spec["template_id"]

    # Cek DULU sebelum nulis apa pun: jangan sampai zip tertulis tapi registrasi ditolak.
    regs = json.loads(Path(args.region_templates).read_text(encoding="utf-8"))
    if tid in regs and not any(m in regs[tid] for m in OWN_MARKERS):
        raise SystemExit(
            f"[build_from_spec] BERHENTI: template_id '{tid}' adalah template ASLI di region_templates.json. "
            f"Tidak akan ditimpa (bisa bikin server gagal konek). Ganti template_id di editor "
            f"(mis. '{tid}_gen01') lalu export ulang.")

    itype = resolve_island_type(spec)
    print(f"[build_from_spec] tipe pulau: {itype}" + ("" if spec.get("island_type") else " (ditebak dari biome dominan)"))
    warn_species_mismatch(spec, itype)
    like_id = args.like_template or pick_like_template(regs, itype, spec["level"])
    print(f"[build_from_spec] kerangka entry dari template asli: {like_id or '(tidak ada, entry minimal)'}")
    entry = build_region_entry(regs, spec, itype, like_id)

    out_zip = Path(args.terrains_dir) / f"{tid}.zip"
    build_zip(spec, out_zip, args.like_template)
    add_region_template(args.region_templates, tid, entry)
    for key in args.archipelago_key:
        alias_archipelago(args.archipelago_templates, key, tid)
    if not args.archipelago_key:
        print("[build_from_spec] archipelago_templates.json tidak disentuh (tanpa --archipelago-key)")


if __name__ == "__main__":
    main()
