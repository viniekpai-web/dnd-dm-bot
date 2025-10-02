# bot.py -- Full DnD DM Bot (Levels 0..11+)
# Features: dice, characters, spells (API), items (API), monsters (API),
# combat tracker, attacks, saves, death saves, inventory, leveling, NPCs, quests, embeds.
#
# Requirements:
# pip install discord.py python-dotenv aiohttp

import os
import re
import json
import random
import asyncio
from typing import Optional, Tuple, List, Dict
import aiohttp
from dotenv import load_dotenv
import discord
from discord.ext import commands

# ---------------------------
# Config & Globals
# ---------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("ERROR: DISCORD_TOKEN tidak ditemukan di .env")
    exit(1)

DATA_FILE = "dnd_data.json"
API_BASE = "https://www.dnd5eapi.co/api"

# Create a single aiohttp session reused by the bot
session: Optional[aiohttp.ClientSession] = None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# In-memory structures; persisted to DATA_FILE
characters: Dict[str, dict] = {}    # guild_id -> name -> data
initiatives: Dict[str, list] = {}   # guild_id -> list of (name, roll)
combats: Dict[str, dict] = {}       # guild_id -> {"turn": int, "order": [entities...]}

# ---------------------------
# Utilities: Persistence
# ---------------------------
def load_data():
    global characters, initiatives, combats
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                obj = json.load(f)
                characters = obj.get("characters", {})
                initiatives = obj.get("initiatives", {})
                combats = obj.get("combats", {})
        except Exception as e:
            print("Gagal load data:", e)
            characters = {}
            initiatives = {}
            combats = {}
    else:
        characters = {}
        initiatives = {}
        combats = {}
    print("Data loaded.")

def save_data():
    obj = {"characters": characters, "initiatives": initiatives, "combats": combats}
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print("Gagal save data:", e)

# Load on startup
load_data()

def get_guild_id(ctx) -> Optional[str]:
    if ctx.guild:
        return str(ctx.guild.id)
    return None

# ---------------------------
# Utilities: Dice parsing / rolling
# ---------------------------
def parse_simple_dice(expr: str) -> Optional[dict]:
    # Accepts forms like: 1d20+5, d20, 2d6-1
    expr = expr.replace(" ", "").lower()
    m = re.fullmatch(r'(\d*)d(\d+)([+-]\d+)?', expr)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1) else 1
    sides = int(m.group(2))
    mod = int(m.group(3)) if m.group(3) else 0
    if n <= 0 or n > 200 or sides <= 0 or sides > 2000:
        return None
    rolls = [random.randint(1, sides) for _ in range(n)]
    total = sum(rolls) + mod
    return {"n": n, "sides": sides, "mod": mod, "rolls": rolls, "total": total}

def roll_expr(expr: str) -> Optional[Tuple[List[int], int]]:
    res = parse_simple_dice(expr)
    if not res:
        return None
    return res["rolls"], res["total"]

# Simpler single-d20 with modifier parser like "d20+5" or "+5" or "5"
def roll_d20_with_mod(expr: str) -> Optional[Tuple[int,int]]:
    expr = expr.strip().lower()
    if expr == "":
        roll = random.randint(1,20); return roll, roll
    # if just number like "5" treat as mod only
    m = re.fullmatch(r'([+-]?\d+)$', expr)
    if m:
        mod = int(m.group(1))
        roll = random.randint(1,20)
        return roll, roll + mod
    # try dice parser
    parsed = parse_simple_dice(expr)
    if parsed:
        return parsed["rolls"][0], parsed["total"]
    return None

# ---------------------------
# HTTP helpers for DnD5e API
# ---------------------------
async def api_get(path: str) -> Optional[dict]:
    global session
    if session is None:
        session = aiohttp.ClientSession()
    url = f"{API_BASE}/{path.lstrip('/')}"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                return await resp.json()
            # else return None
            return None
    except Exception as e:
        print("API GET error:", e)
        return None

async def api_list(endpoint: str) -> List[dict]:
    res = await api_get(endpoint)
    if not res:
        return []
    return res.get("results", [])

# Normalize string to slug expected by API
def to_api_slug(name: str) -> str:
    return name.strip().lower().replace(" ", "-")

# ---------------------------
# Character generation & management
# ---------------------------
# base abilities
ABILITY_ORDER = ["STR","DEX","CON","INT","WIS","CHA"]
BASE_STATS = {a: 10 for a in ABILITY_ORDER}

# Some local race/class fallbacks in case API returns sparse info
LOCAL_RACE_MODS = {
    "elf": {"DEX": 2, "INT": 1},
    "dwarf": {"CON": 2, "STR": 1},
    "human": {"STR":1,"DEX":1,"CON":1,"INT":1,"WIS":1,"CHA":1},
    "halfling": {"DEX":2,"CHA":1},
    "orc": {"STR":2,"CON":1,"INT":-1}
}
# Class focus / hp die approximate to influence HP/AC and primary stat
CLASS_FOCUS = {
    "wizard": {"primary": "INT", "hd": 6, "ac": 12},
    "sorcerer": {"primary":"CHA","hd":6,"ac":12},
    "warlock": {"primary":"CHA","hd":8,"ac":13},
    "cleric": {"primary":"WIS","hd":8,"ac":15},
    "fighter": {"primary":"STR","hd":10,"ac":16},
    "rogue": {"primary":"DEX","hd":8,"ac":14},
    "ranger": {"primary":"DEX","hd":10,"ac":15},
    "paladin": {"primary":"STR","hd":10,"ac":18},
    "barbarian": {"primary":"STR","hd":12,"ac":14},
}

# Spell slot defaults by class (simple)
CLASS_SPELL_SLOTS = {
    "wizard": {1:2, 2:0, 3:0},
    "sorcerer": {1:2},
    "cleric": {1:2},
    "warlock": {1:1},
    "paladin": {},
    "fighter": {},
    "rogue": {}
}

async def get_race_bonuses_from_api(race_name: str) -> dict:
    slug = to_api_slug(race_name)
    data = await api_get(f"races/{slug}")
    if not data:
        return {}
    # API returns array of ability bonuses like {"ability_score":{"name":"Dexterity"},"bonus":2}
    bonuses = {}
    for ab in data.get("ability_bonuses", []):
        name = ab.get("ability_score", {}).get("name", "")
        if name:
            key = name[:3].upper()  # "Dexterity" -> "Dex" -> "DEX"
            # Try mapping: Dexterity -> DEX, Strength -> STR etc.
            # Safer mapping:
            mapping = {"Strength":"STR","Dexterity":"DEX","Constitution":"CON","Intelligence":"INT","Wisdom":"WIS","Charisma":"CHA"}
            full = ab.get("ability_score", {}).get("name")
            key = mapping.get(full, full[:3].upper())
            bonuses[key] = bonuses.get(key, 0) + ab.get("bonus", 0)
    return bonuses

# pick some random skills from API or fallback to static
async def pick_skills_for_class(cls: str, count=2) -> List[str]:
    # try querying class endpoint for proficiencies? simplified: use API skills list and pick random
    skills_list = await api_list("skills")
    skill_names = [s["name"] for s in skills_list] if skills_list else []
    if not skill_names:
        # local fallback
        fallback = {
            "wizard":["Arcana","History","Investigation"],
            "fighter":["Athletics","Survival","Intimidation"],
            "rogue":["Stealth","Acrobatics","Sleight of Hand"],
            "cleric":["Religion","Insight","Medicine"]
        }
        return fallback.get(cls.lower(), ["Perception","Athletics"])[:count]
    return random.sample(skill_names, min(count, len(skill_names)))

# get spells list (names) from API and filter by class
async def pick_spells_for_class(cls: str, count=3) -> List[str]:
    spells = await api_list("spells")
    if not spells:
        return []
    # simple heuristic: pick spells whose description or name includes class (not robust)
    names = [s["name"] for s in spells]
    # try to prioritize low-level spells for casters
    # simpler: pick random subset
    picks = random.sample(names, min(count, len(names)))
    return picks

async def generate_character(gid: str, name: str, race: str, cls: str) -> dict:
    gid = str(gid)
    race_bonuses = await get_race_bonuses_from_api(race)
    # apply base + race + class focus
    stats = BASE_STATS.copy()
    for k,v in race_bonuses.items():
        if k in stats:
            stats[k] += v
    # apply local fallback for class primary
    cf = CLASS_FOCUS.get(cls.lower(), {})
    if "primary" in cf:
        stats[cf["primary"]] = stats.get(cf["primary"],10) + 2
    # HP and AC
    hp = cf.get("hd", 8)
    ac = cf.get("ac", 10)
    # pick skills & spells
    skills = await pick_skills_for_class(cls, count=3)
    spells = await pick_spells_for_class(cls, count=4)
    # initialize inventory & slots & level
    slots = CLASS_SPELL_SLOTS.get(cls.lower(), {}).copy() if CLASS_SPELL_SLOTS.get(cls.lower()) else {}
    char = {
        "name": name,
        "race": race,
        "class": cls,
        "level": 1,
        "hp": hp,
        "max_hp": hp,
        "ac": ac,
        "stats": stats,
        "skills": skills,
        "spells": spells,
        "slots": slots,
        "inventory": [],
        "death_saves": {"success":0, "failure":0},
    }
    characters.setdefault(gid, {})
    characters[gid][name.lower()] = char
    save_data()
    return char

# ---------------------------
# Spell detail & casting using API
# ---------------------------
async def fetch_spell_detail(spell_name: str) -> Optional[dict]:
    slug = to_api_slug(spell_name)
    return await api_get(f"spells/{slug}")

# Parse damage string from API detail (damage_at_slot_level or damage at character level)
def get_damage_expr_from_spell(detail: dict, slot_level: Optional[int]) -> Optional[str]:
    damage = detail.get("damage", {})
    # damage_at_slot_level is a dict mapping slot level to dice expression
    if isinstance(damage.get("damage_at_slot_level"), dict) and slot_level:
        return damage["damage_at_slot_level"].get(str(slot_level))
    # damage_at_character_level mapping (strings of character level)
    if isinstance(damage.get("damage_at_character_level"), dict):
        vals = list(damage["damage_at_character_level"].values())
        if vals:
            return vals[0]
    # fallback: sometimes 'damage' has 'damage_at_slot_level' as empty; none
    return None

# ---------------------------
# Spell casting management
# ---------------------------
async def cast_spell_for_char(gid: str, name: str, spell_name: str, use_slot_level: Optional[int]=None) -> Tuple[str, bool]:
    gid = str(gid)
    char = characters.get(gid, {}).get(name.lower())
    if not char:
        return f"Karakter {name} tidak ditemukan.", False
    # check if spell in char spells
    found = None
    for s in char.get("spells", []):
        if s.lower() == spell_name.lower():
            found = s
            break
    if not found:
        return f"{char['name']} tidak memiliki spell {spell_name}.", False

    detail = await fetch_spell_detail(found)
    if not detail:
        return f"Tidak dapat mengambil data spell '{found}' dari API.", False

    level = detail.get("level", 0)
    # if spell uses slots (level > 0), check slots available
    if level > 0:
        slots = char.get("slots", {})
        available = slots.get(level, 0)
        if available <= 0:
            return f"{char['name']} tidak punya slot level {level} untuk cast {found}.", False
        # consume slot
        slots[level] = available - 1
        save_data()

    # get damage expression
    dmg_expr = get_damage_expr_from_spell(detail, use_slot_level or level)
    # roll attack? For simplicity, we'll show attack roll as d20 (casters may need attack or save)
    atk_roll = random.randint(1,20)
    msg = f"✨ {char['name']} melempar spell **{found}** (level {level})\n"
    msg += f"🎲 Attack/Effect roll (d20): **{atk_roll}**\n"
    if dmg_expr:
        parsed = parse_simple_dice(dmg_expr)
        if parsed:
            rolls = parsed["rolls"]; total = parsed["total"]
            msg += f"💥 Damage: {dmg_expr} → {rolls} = **{total}**\n"
        else:
            msg += f"ℹ Damage expression: {dmg_expr}\n"
    else:
        desc = detail.get("desc", [])
        if isinstance(desc, list):
            msg += f"ℹ {desc[0]}\n"
        else:
            msg += f"ℹ {desc}\n"
    return msg, True

# ---------------------------
# Inventory & Items (API)
# ---------------------------
async def fetch_item_detail(item_name: str) -> Optional[dict]:
    slug = to_api_slug(item_name)
    return await api_get(f"equipment/{slug}")

# ---------------------------
# Leveling system
# ---------------------------
LEVEL_UP_HP = {
    "wizard": 4,
    "sorcerer": 6,
    "warlock": 8,
    "cleric": 6,
    "fighter": 10,
    "rogue": 8,
    "paladin": 10,
    "ranger": 10,
    "barbarian": 12
}

def get_con_mod(stat_value: int) -> int:
    return (stat_value - 10) // 2

def level_up_character(gid: str, name: str) -> Tuple[str,bool]:
    gid = str(gid)
    char = characters.get(gid, {}).get(name.lower())
    if not char:
        return f"Karakter {name} tidak ditemukan.", False
    cls = char.get("class","").lower()
    cur_level = char.get("level",1)
    new_level = cur_level + 1
    char["level"] = new_level
    # HP gain random roll by class hit die + CON mod
    hd = LEVEL_UP_HP.get(cls, 6)
    hp_gain = random.randint(1, hd) + get_con_mod(char["stats"].get("CON",10))
    if hp_gain < 1: hp_gain = 1
    char["max_hp"] = char.get("max_hp", char.get("hp",10)) + hp_gain
    char["hp"] = char["max_hp"]
    # simple rule: add +1 slot to level 1 if class has slots
    if cls in CLASS_SPELL_SLOTS:
        slots = char.setdefault("slots", {})
        slots[1] = slots.get(1,0) + 1
    save_data()
    return f"⬆️ {char['name']} naik ke level {new_level}! +{hp_gain} HP (Total {char['max_hp']})", True

# ---------------------------
# Combat tracker and actions (Level 5+)
# ---------------------------
def ensure_combat(gid: str):
    gid = str(gid)
    if gid not in combats:
        combats[gid] = {"turn": 0, "order": []}
        save_data()

@bot.command(name="combat_start")
async def cmd_combat_start(ctx):
    gid = get_guild_id(ctx)
    ensure_combat(gid)
    combats[str(gid)]["turn"] = 0
    combats[str(gid)]["order"] = []
    save_data()
    await ctx.send("⚔️ Encounter dimulai. Gunakan `!combat_add <name> <hp> <initiative> [ac]` untuk menambah participant.")

@bot.command(name="combat_add")
async def cmd_combat_add(ctx, name: str, hp: int, initiative: int, ac: Optional[int]=None):
    gid = get_guild_id(ctx)
    if not gid:
        return await ctx.send("Perintah hanya di server.")
    ensure_combat(gid)
    ent = {"name": name, "hp": int(hp), "initiative": int(initiative), "effects": [], "ac": int(ac) if ac else 10}
    combats[str(gid)]["order"].append(ent)
    combats[str(gid)]["order"].sort(key=lambda x: x["initiative"], reverse=True)
    save_data()
    await ctx.send(f"➕ {name} ditambahkan ke encounter (HP {hp}, Init {initiative}, AC {ent['ac']}).")

@bot.command(name="combat_status")
async def cmd_combat_status(ctx):
    gid = get_guild_id(ctx)
    if not gid or str(gid) not in combats or not combats[str(gid)]["order"]:
        return await ctx.send("Belum ada encounter aktif.")
    c = combats[str(gid)]
    turn = c["turn"]
    msg_lines = ["📜 **Status Encounter**"]
    for i, e in enumerate(c["order"]):
        active = "➡️" if i == turn else "  "
        effects = f" ({', '.join(e['effects'])})" if e.get("effects") else ""
        msg_lines.append(f"{active} {e['name']} — HP: {e['hp']} | Init: {e['initiative']} | AC: {e.get('ac',10)}{effects}")
    await ctx.send("\n".join(msg_lines))

@bot.command(name="combat_next")
async def cmd_combat_next(ctx):
    gid = get_guild_id(ctx)
    if not gid or str(gid) not in combats or not combats[str(gid)]["order"]:
        return await ctx.send("Belum ada encounter aktif.")
    c = combats[str(gid)]
    c["turn"] = (c["turn"] + 1) % len(c["order"])
    save_data()
    cur = c["order"][c["turn"]]
    await ctx.send(f"➡️ Sekarang giliran **{cur['name']}** (HP {cur['hp']})")

@bot.command(name="combat_effect")
async def cmd_combat_effect(ctx, name: str, *, effect: str):
    gid = get_guild_id(ctx)
    if not gid or str(gid) not in combats:
        return await ctx.send("Tidak ada encounter aktif.")
    for ent in combats[str(gid)]["order"]:
        if ent["name"].lower() == name.lower():
            ent.setdefault("effects", []).append(effect)
            save_data()
            return await ctx.send(f"💫 Efek **{effect}** ditambahkan ke {ent['name']}.")
    await ctx.send(f"❌ {name} tidak ditemukan di encounter.")

@bot.command(name="combat_damage")
async def cmd_combat_damage(ctx, name: str, amount: int):
    gid = get_guild_id(ctx)
    if not gid or str(gid) not in combats:
        return await ctx.send("Tidak ada encounter aktif.")
    for ent in combats[str(gid)]["order"]:
        if ent["name"].lower() == name.lower():
            ent["hp"] -= int(amount)
            save_data()
            msg = f"💥 {ent['name']} menerima {amount} damage. HP sekarang: {ent['hp']}"
            if ent["hp"] <= 0:
                msg += f"\n☠️ {ent['name']} berada di 0 HP!"
                # if it's a PC stored in characters, set death state
                gidstr = str(gid)
                char = characters.get(gidstr, {}).get(name.lower())
                if char:
                    # mark hp at 0 and reset death saves
                    char["hp"] = 0
                    char["death_saves"] = {"success":0,"failure":0}
                    save_data()
            return await ctx.send(msg)
    await ctx.send(f"❌ {name} tidak ditemukan di encounter.")

@bot.command(name="combat_heal")
async def cmd_combat_heal(ctx, name: str, amount: int):
    gid = get_guild_id(ctx)
    if not gid or str(gid) not in combats:
        return await ctx.send("Tidak ada encounter aktif.")
    for ent in combats[str(gid)]["order"]:
        if ent["name"].lower() == name.lower():
            ent["hp"] += int(amount)
            save_data()
            return await ctx.send(f"✨ {ent['name']} dipulihkan {amount} HP. HP sekarang: {ent['hp']}")
    await ctx.send(f"❌ {name} tidak ditemukan di encounter.")

@bot.command(name="combat_setac")
async def cmd_combat_setac(ctx, name: str, ac: int):
    gid = get_guild_id(ctx)
    if not gid or str(gid) not in combats:
        return await ctx.send("Tidak ada encounter aktif.")
    for ent in combats[str(gid)]["order"]:
        if ent["name"].lower() == name.lower():
            ent["ac"] = int(ac)
            save_data()
            return await ctx.send(f"🛡️ AC {ent['name']} di-set ke {ac}.")
    await ctx.send(f"❌ {name} tidak ditemukan di encounter.")

@bot.command(name="combat_end")
async def cmd_combat_end(ctx):
    gid = get_guild_id(ctx)
    if not gid or str(gid) not in combats:
        return await ctx.send("Tidak ada encounter aktif.")
    combats.pop(str(gid), None)
    save_data()
    await ctx.send("🏁 Encounter diakhiri.")

# ---------------------------
# Attack & Saving Throws (Level 6)
# ---------------------------
@bot.command(name="attack")
async def cmd_attack(ctx, attacker: str, target: str, roll_expr: str):
    gid = get_guild_id(ctx)
    if not gid or str(gid) not in combats:
        return await ctx.send("Tidak ada encounter aktif.")
    order = combats[str(gid)]["order"]
    atk = next((x for x in order if x["name"].lower() == attacker.lower()), None)
    tgt = next((x for x in order if x["name"].lower() == target.lower()), None)
    if not atk or not tgt:
        return await ctx.send("Attacker atau target tidak ditemukan dalam encounter.")
    parsed = parse_simple_dice(roll_expr)
    if not parsed:
        return await ctx.send("Format roll salah. Contoh: `d20+5` atau `1d20+4`.")
    # For attack expressions where user included modifier, parsed total is fine
    total = parsed["total"]
    rolls = parsed["rolls"]
    ac = tgt.get("ac", 10)
    hit = total >= ac
    msg = f"⚔️ **{atk['name']}** menyerang **{tgt['name']}**!\n🎲 Attack roll {roll_expr} → {rolls} = **{total}**\nAC target: **{ac}**\n"
    if hit:
        msg += "✅ **HIT!**\n"
        # Optionally roll damage: ask for damage expr after or require attacker to supply earlier.
        # Here we don't auto roll damage unless attacker supplies a damage expr; keep simple.
    else:
        msg += "❌ Miss.\n"
    await ctx.send(msg)

@bot.command(name="save")
async def cmd_save(ctx, name: str, ability: str, dc: int):
    gid = get_guild_id(ctx)
    if not gid or str(gid) not in combats:
        return await ctx.send("Tidak ada encounter aktif.")
    ent = next((x for x in combats[str(gid)]["order"] if x["name"].lower() == name.lower()), None)
    if not ent:
        return await ctx.send(f"{name} tidak ditemukan.")
    ability = ability.lower()
    mapping = {"str":"STR","dex":"DEX","con":"CON","int":"INT","wis":"WIS","cha":"CHA"}
    if ability not in mapping:
        return await ctx.send("Ability tidak valid (gunakan str/dex/con/int/wis/cha).")
    # find if entity is a PC with stats
    char = characters.get(str(gid), {}).get(name.lower())
    mod = 0
    if char:
        val = char["stats"].get(mapping[ability], 10)
        mod = (val - 10) // 2
    roll = random.randint(1,20) + mod
    success = roll >= dc
    await ctx.send(f"🛡️ **{name}** melakukan saving throw {mapping[ability]} (DC {dc}): d20 + mod = **{roll}** → {'✅ Success' if success else '❌ Failed'}")

# ---------------------------
# Death saves (Level 7)
# ---------------------------
@bot.command(name="deathsave")
async def cmd_deathsave(ctx, name: str):
    gid = get_guild_id(ctx)
    gidstr = str(gid)
    char = characters.get(gidstr, {}).get(name.lower())
    if not char:
        return await ctx.send(f"Karakter {name} tidak ditemukan.")
    if char.get("hp",1) > 0:
        return await ctx.send(f"{char['name']} tidak berada di 0 HP.")
    roll = random.randint(1,20)
    if roll == 20:
        # regain 1 HP and stabilize
        char["hp"] = 1
        char["death_saves"] = {"success":0,"failure":0}
        save_data()
        return await ctx.send(f"🎉 Natural 20! {char['name']} bangkit dengan 1 HP.")
    elif roll == 1:
        char["death_saves"]["failure"] += 2
    elif roll >= 10:
        char["death_saves"]["success"] += 1
    else:
        char["death_saves"]["failure"] += 1
    save_data()
    ds = char["death_saves"]
    if ds["success"] >= 3:
        # stabilized but still at 0 HP; treat as stable but unconscious
        char["death_saves"] = {"success":0,"failure":0}
        save_data()
        return await ctx.send(f"✅ {char['name']} berhasil stabil (3 success).")
    if ds["failure"] >= 3:
        # character dies
        # remove or mark dead
        del characters[gidstr][name.lower()]
        save_data()
        return await ctx.send(f"☠️ {name} gagal death saves 3x — meninggal.")
    await ctx.send(f"🎲 Death Save roll: {roll} → Successes: {ds['success']} | Failures: {ds['failure']}")

# ---------------------------
# Monster lookup (API)
# ---------------------------
@bot.command(name="monster")
async def cmd_monster(ctx, *, monster_name: str):
    detail = await api_get(f"monsters/{to_api_slug(monster_name)}")
    if not detail:
        return await ctx.send(f"❌ Monster '{monster_name}' tidak ditemukan di API.")
    name = detail.get("name")
    hp = detail.get("hit_points")
    ac = None
    ac_raw = detail.get("armor_class")
    if isinstance(ac_raw, list) and ac_raw:
        ac = ac_raw[0].get("value") if isinstance(ac_raw[0], dict) else ac_raw[0]
    elif isinstance(ac_raw, dict):
        ac = ac_raw.get("value")
    else:
        ac = ac_raw
    cr = detail.get("challenge_rating")
    type_ = detail.get("type")
    desc = detail.get("special_abilities",[{"desc":"No description"}])
    desc_text = desc[0].get("desc") if isinstance(desc, list) and desc else "No description"
    await ctx.send(f"👹 **{name}**\nType: {type_} | CR: {cr}\nHP: {hp} | AC: {ac}\n{desc_text}")

# ---------------------------
# Inventory commands & API item
# ---------------------------
@bot.command(name="inventory_add")
async def cmd_inv_add(ctx, name: str, *, item: str):
    gid = get_guild_id(ctx)
    gidstr = str(gid)
    char = characters.get(gidstr, {}).get(name.lower())
    if not char:
        return await ctx.send(f"Karakter {name} tidak ditemukan.")
    char.setdefault("inventory", []).append({"name": item})
    save_data()
    await ctx.send(f"👜 {item} ditambahkan ke inventori {char['name']}.")

@bot.command(name="inventory_add_api")
async def cmd_inv_add_api(ctx, name: str, *, item_name: str):
    gid = get_guild_id(ctx)
    gidstr = str(gid)
    char = characters.get(gidstr, {}).get(name.lower())
    if not char:
        return await ctx.send(f"Karakter {name} tidak ditemukan.")
    detail = await fetch_item_detail(item_name)
    if not detail:
        return await ctx.send(f"❌ Item '{item_name}' tidak ditemukan di API.")
    item_obj = {"name": detail.get("name","Unknown"), "desc": detail.get("desc",[])}
    char.setdefault("inventory", []).append(item_obj)
    save_data()
    await ctx.send(f"👜 {item_obj['name']} ditambahkan ke inventori {char['name']} (dari API).")

@bot.command(name="inventory_list")
async def cmd_inv_list(ctx, name: str):
    gid = get_guild_id(ctx)
    gidstr = str(gid)
    char = characters.get(gidstr, {}).get(name.lower())
    if not char:
        return await ctx.send(f"Karakter {name} tidak ditemukan.")
    inv = char.get("inventory", [])
    if not inv:
        return await ctx.send(f"📦 Inventori {char['name']} kosong.")
    out = "\n".join([f"- {i.get('name')}" if isinstance(i, dict) else f"- {i}" for i in inv])
    await ctx.send(f"👜 Inventori {char['name']}:\n{out}")

@bot.command(name="inventory_remove")
async def cmd_inv_remove(ctx, name: str, *, item: str):
    gid = get_guild_id(ctx)
    gidstr = str(gid)
    char = characters.get(gidstr, {}).get(name.lower())
    if not char:
        return await ctx.send(f"Karakter {name} tidak ditemukan.")
    inv = char.get("inventory", [])
    found_index = None
    for i,entry in enumerate(inv):
        if (isinstance(entry,dict) and entry.get("name","").lower() == item.lower()) or (isinstance(entry,str) and entry.lower()==item.lower()):
            found_index = i; break
    if found_index is None:
        return await ctx.send(f"{char['name']} tidak memiliki item {item}.")
    removed = inv.pop(found_index)
    save_data()
    await ctx.send(f"🗑️ {removed.get('name',removed)} dihapus dari inventori {char['name']}.")

# ---------------------------
# Character create, status, sheet embed
# ---------------------------
def build_character_embed(char: dict) -> discord.Embed:
    embed = discord.Embed(title=f"{char['name']} — {char.get('race','')} {char.get('class','')}", color=discord.Color.blue())
    embed.add_field(name="Level", value=str(char.get("level",1)), inline=True)
    embed.add_field(name="HP", value=f"{char.get('hp')}/{char.get('max_hp',char.get('hp'))}", inline=True)
    embed.add_field(name="AC", value=str(char.get("ac",10)), inline=True)
    # stats
    stats_text = "\n".join([f"**{k}**: {v}" for k,v in char.get("stats",{}).items()])
    embed.add_field(name="Stats", value=stats_text, inline=False)
    # skills
    embed.add_field(name="Skills", value=", ".join(char.get("skills",[])) or "None", inline=False)
    # spells
    embed.add_field(name="Spells", value=", ".join(char.get("spells",[])) or "None", inline=False)
    # slots
    slots = char.get("slots",{})
    if slots:
        embed.add_field(name="Spell Slots", value="\n".join([f"Level {k}: {v}" for k,v in slots.items()]), inline=False)
    # inventory
    inv = char.get("inventory",[])
    if inv:
        inv_text = ", ".join([i.get("name") if isinstance(i,dict) else str(i) for i in inv])
        embed.add_field(name="Inventory", value=inv_text, inline=False)
    return embed

@bot.command(name="char_create")
async def cmd_char_create(ctx, name: str, race: str, cls: str):
    gid = get_guild_id(ctx)
    if not gid:
        return await ctx.send("Command hanya boleh dipakai di server.")
    char = await generate_character(gid, name, race, cls)
    # ensure max_hp present
    char.setdefault("max_hp", char.get("hp",10))
    save_data()
    embed = build_character_embed(char)
    await ctx.send("🧙 Karakter dibuat:", embed=embed)

@bot.command(name="char_status")
async def cmd_char_status(ctx, name: str):
    gid = get_guild_id(ctx)
    gidstr = str(gid)
    char = characters.get(gidstr, {}).get(name.lower())
    if not char:
        return await ctx.send(f"Karakter {name} tidak ditemukan.")
    embed = build_character_embed(char)
    await ctx.send(embed=embed)

# ---------------------------
# Spell commands
# ---------------------------
@bot.command(name="slots")
async def cmd_slots(ctx, name: str):
    gid = get_guild_id(ctx)
    char = characters.get(str(gid), {}).get(name.lower())
    if not char:
        return await ctx.send("Karakter tidak ditemukan.")
    slots = char.get("slots",{})
    if not slots:
        return await ctx.send(f"{char['name']} tidak memiliki slot spell.")
    txt = "\n".join([f"Level {k}: {v}" for k,v in slots.items()])
    await ctx.send(f"🔮 Slot untuk {char['name']}:\n{txt}")

@bot.command(name="longrest")
async def cmd_longrest(ctx, name: str):
    gid = get_guild_id(ctx)
    char = characters.get(str(gid), {}).get(name.lower())
    if not char:
        return await ctx.send("Karakter tidak ditemukan.")
    # reset slots to class defaults
    cls = char.get("class","").lower()
    char["slots"] = CLASS_SPELL_SLOTS.get(cls, {}).copy()
    char["hp"] = char.get("max_hp", char.get("hp",10))
    save_data()
    await ctx.send(f"😴 {char['name']} melakukan long rest: HP dan slot dipulihkan.")

@bot.command(name="cast")
async def cmd_cast(ctx, name: str, *, spell: str):
    gid = get_guild_id(ctx)
    if not gid:
        return await ctx.send("Perintah hanya di server.")
    msg, ok = await cast_spell_for_char(gid, name, spell)
    await ctx.send(msg)

# ---------------------------
# NPC / Quest generator (simple)
# ---------------------------
NPC_NAMES = ["Elandra","Borric","Mira","Galen","Thorin","Lysa","Keth","Asha","Roran","Isolde"]
NPC_ROLES = ["blacksmith","innkeeper","merchant","wizard","priest","ranger","thief","noble"]
QUEST_HOOKS = [
    "Sebuah desa dikepung makhluk malam.",
    "Sebuah artefak kuno hilang dari kuil.",
    "Seorang bangsawan meminta bantuan untuk menemukan adiknya.",
    "Kawanan goblin mencuri ternak.",
    "Reruntuhan di pegunungan memancarkan cahaya aneh."
]

@bot.command(name="npc")
async def cmd_npc(ctx, *, role: Optional[str]=None):
    name = random.choice(NPC_NAMES)
    role_choice = role or random.choice(NPC_ROLES)
    personality = random.choice(["friendly","grumpy","mysterious","talkative","secretive"])
    secret = random.choice(["works for thieves' guild","hides a magical scar","is actually a refugee","is cursed"])
    await ctx.send(f"🎭 NPC: **{name}** — {role_choice}\nPersonality: {personality}\nSecret: {secret}")

@bot.command(name="quest")
async def cmd_quest(ctx):
    hook = random.choice(QUEST_HOOKS)
    twist = random.choice(["pihak yang disangka musuh sebenarnya korban","penjaga kuil adalah ras kuno","ada jebakan waktu di situs"])
    location = random.choice(["hutan terlarang","desa nelayan","reruntuhan gua","kuil terpencil"])
    await ctx.send(f"🗺️ Quest Hook: {hook}\nLocation: {location}\nTwist: {twist}")

# ---------------------------
# Skill check
# ---------------------------
@bot.command(name="skill")
async def cmd_skill(ctx, name: str, *, skill: str):
    gid = get_guild_id(ctx)
    char = characters.get(str(gid), {}).get(name.lower())
    if not char:
        return await ctx.send("Karakter tidak ditemukan.")
    # if skill in list -> give proficiency bonus (simple +2)
    bonus = 2 if skill.title() in char.get("skills",[]) else 0
    roll = random.randint(1,20)
    total = roll + bonus
    await ctx.send(f"🎲 {char['name']} melakukan check **{skill.title()}** → d20({roll}) + bonus({bonus}) = **{total}**")

# ---------------------------
# Level up command
# ---------------------------
@bot.command(name="levelup")
async def cmd_levelup(ctx, name: str):
    gid = get_guild_id(ctx)
    msg, ok = level_up_character(gid, name)
    await ctx.send(msg)

# ---------------------------
# Misc: help
# ---------------------------
@bot.command(name="help")
async def cmd_help(ctx):
    h = (
        "**Perintah Utama**\n"
        "`!help` - tampilkan pesan ini\n"
        "`!roll <XdY+Z>` - lempar dadu\n"
        "`!char_create <name> <race> <class>` - auto-generate character\n"
        "`!char_status <name>` / `!sheet <name>` - tampilkan sheet\n"
        "`!levelup <name>` - naik level\n"
        "`!inventory_add <name> <item>` / `!inventory_add_api <name> <item>` / `!inventory_list <name>` / `!inventory_remove <name> <item>`\n"
        "`!cast <name> <spell>` / `!slots <name>` / `!longrest <name>`\n"
        "`!combat_start` / `!combat_add <name> <hp> <init> [ac]` / `!combat_status` / `!combat_next` / `!combat_damage` / `!combat_heal` / `!combat_setac` / `!combat_end`\n"
        "`!attack <attacker> <target> <d20+mod>` / `!save <name> <ability> <DC>` / `!deathsave <name>`\n"
        "`!monster <name>` / `!npc` / `!quest` / `!skill <name> <skill>`\n"
    )
    await ctx.send(h)

# ---------------------------
# Startup / Shutdown
# ---------------------------
@bot.event
async def on_ready():
    global session
    if session is None:
        session = aiohttp.ClientSession()
    print(f"Bot siap sebagai {bot.user} (ID: {bot.user.id})")
    # ensure data saved on start
    save_data()

@bot.event
async def on_disconnect():
    # close session
    global session
    if session:
        asyncio.create_task(session.close())
        session = None

# Graceful shutdown helper (optional)
async def shutdown():
    global session
    save_data()
    if session:
        await session.close()

# Run
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    finally:
        # ensure save on exit
        save_data()
