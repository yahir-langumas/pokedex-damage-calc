from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from .damage_calculator import PHYSICAL_DEFENSE_SPECIAL_MOVES
from .forms import ITEMS, STAT_KEYS, STAT_LABELS, TEAM_SIZE, DamageCalcForm, TeamForm, pretty, slug
from .models import Learnset, Moves, Species
from .stats import DEFAULT_LEVEL, NATURES, calculate_stats


# Shows names the way people read them, like "Choice Scarf" instead of "choice-scarf". The forms turn them back into database names.
def display_names(data, fields):
    return {name: pretty(slug(value)) if name in fields else value for name, value in data.items()}


# Every Pokémon's name for the name boxes, like "Iron Valiant".
def species_names():
    return [pretty(name) for name in Species.objects.order_by("name").values_list("name", flat=True)]


def home(request):
    counts = {"species_count": Species.objects.count(), "moves_count": Moves.objects.count(), "items_count": len(ITEMS)}
    return render(request, "home.html", {name: f"{count:,}" for name, count in counts.items()})


def pokemon_table(request):
    species_list = Species.objects.all().order_by("pokedex_id")
    return render(request, "pokemon_table.html", {"species_list": species_list})


# The team builder page. Pick up to six Pokémon, and the URL saves the team so it can be shared.
def team_builder(request):
    form = TeamForm(display_names(request.GET.dict(), [f"pokemon_{number}" for number in range(1, TEAM_SIZE + 1)]))
    form.is_valid()
    team = form.cleaned_data["team"]
    slots = [{"number": number, "field": form[f"pokemon_{number}"], "species": species, "name": pretty(species.name) if species else None, "stats": base_stat_rows(species)}
             for number, species in enumerate(team, start=1)]
    context = {
        "slots": slots,
        "team_count": sum(species is not None for species in team),
        "team_size": TEAM_SIZE,
        "species_names": species_names(),
    }
    return render(request, "team_builder.html", context)


# A Pokémon's base stats for the team builder, with bar widths out of 255 (the highest base stat).
def base_stat_rows(species):
    if species is None:
        return []
    return [{"label": label, "value": species.stats[stat], "width": round(species.stats[stat] / 255 * 100)} for stat, label in STAT_LABELS.items()]

def moves(request):
    moves_list = Moves.objects.all().order_by("id")
    return render(request, "moves.html", {"moves_list": moves_list})


# The damage calculator page. Reads the form from the URL, runs calculate() and shows the dmg rolls, % and KO chance.
def damage_calculator(request):
    data = DamageCalcForm.default_data()
    data.update(request.GET.dict())
    form = DamageCalcForm(display_names(data, [f"{side}_{field}" for side in ("attacker", "defender") for field in ("pokemon", "item")]))
    result = description = hp_bar = None
    if form.is_valid():
        calc = form.make_calculator()
        result = calc.calculate(**form.calc_options(calc))
        description = describe_calc(form.cleaned_data, result)
        hp_bar = hp_bar_widths(result)
    attacker = form.cleaned_data.get("attacker_species")
    context = {
        "form": form,
        "result": result,
        "description": description,
        "hp_bar": hp_bar,
        "attacker": side_details(form, "attacker"),
        "defender": side_details(form, "defender"),
        "moves": move_choices(attacker, form.cleaned_data.get("move_data")) if attacker else [],
        "selected_move": form.data.get("move"),
        "species_names": species_names(),
        "items": [pretty(item) for item in ITEMS],
        "effectiveness": effectiveness_text(result["effectiveness"]) if result else None,
    }
    return render(request, "damage_calculator.html", context)


# A Pokémon's abilities and damaging moves. The calc page uses this to fill in the dropdowns when a Pokémon is picked.
def pokemon_options(request, name):
    species = get_object_or_404(Species, name=name.strip().lower())
    abilities = [{"name": ability, "label": pretty(ability)} for ability in species.abilities]
    moves = [{"name": move.name, "label": pretty(move.name)} for move in damaging_moves(species)]
    return JsonResponse({"name": species.name, "abilities": abilities, "moves": moves})


# The damaging moves a Pokémon can learn, sorted by name. Falls back to every damaging move if it has none.
def damaging_moves(species):
    moves = Moves.objects.exclude(damage_class="status").order_by("name")
    learnset = Learnset.objects.filter(species_id=species.pokedex_id).first()
    learned = moves.filter(name__in=learnset.moves) if learnset else moves.none()
    return learned if learned.exists() else moves


# The attacker's move dropdown. A move it can't learn still shows up if it was picked (from a shared link, for example).
def move_choices(species, picked_move):
    choices = [(move.name, pretty(move.name)) for move in damaging_moves(species)]
    if picked_move and picked_move.name not in [name for name, label in choices]:
        choices.insert(0, (picked_move.name, pretty(picked_move.name)))
    return choices


# One side's Pokémon, ability dropdown and stat table (base stat, IVs, EVs, stat stage and real stat).
def side_details(form, side):
    species = form.cleaned_data.get(f"{side}_species")
    build = form.cleaned_data.get(f"{side}_build")
    stats = calculate_stats(species.stats, build, species.pokedex_id) if species and build else {}
    raised, lowered = NATURES[build["nature"]] if build else (None, None)
    rows = []
    for key, stat in STAT_KEYS.items():
        rows.append({"label": STAT_LABELS[stat], "base": species.stats[stat] if species else "", "stat": stats.get(stat, ""),
                     "nature": "+" if stat == raised else "-" if stat == lowered else "",
                     "iv": form[f"{side}_iv_{key}"], "ev": form[f"{side}_ev_{key}"], "boost": form[f"{side}_boost_{key}"] if key != "hp" else None})
    abilities = species.abilities if species else []
    picked = form.data.get(f"{side}_ability")
    fields = {name: form[f"{side}_{name}"] for name in ("pokemon", "item", "level", "nature", "status", "hp_percent")}
    return {"species": species, "name": pretty(species.name) if species else "", "rows": rows, "fields": fields, "ev_total": sum(form.cleaned_data.get(f"{side}_ev_{key}") or 0 for key in STAT_KEYS),
            "abilities": [(ability, pretty(ability)) for ability in abilities], "ability": picked if picked in abilities else (abilities[0] if abilities else "")}


# A one-line summary of the calc like Smogon's, such as "252+ Atk Choice Band Garchomp Earthquake vs. 252 HP / 0 Def Snorlax: 100-118 (42.5 - 50.2%) -- Possible 2HKO".
def describe_calc(data, result):
    move = data["move_data"]
    physical = result["category"] == "physical"
    attack_stat = None if move.name == "foul-play" else "defense" if move.name == "body-press" else "attack" if physical else "special-attack"
    defense_stat = "defense" if physical or move.name in PHYSICAL_DEFENSE_SPECIAL_MOVES else "special-defense"
    attacker = [boost_text(data, "attacker", attack_stat), ev_text(data["attacker_build"], attack_stat), level_text(data["attacker_build"]),
                pretty(data["attacker_item"]), pretty(data["attacker_species"].name), pretty(move.name)]
    defender = [boost_text(data, "defender", defense_stat), f"{data['defender_build']['evs']['hp']} HP /", ev_text(data["defender_build"], defense_stat),
                level_text(data["defender_build"]), pretty(data["defender_item"]), pretty(data["defender_species"].name)]
    extras = []
    if data["weather"]:
        extras.append(f"in {pretty(data['weather'])}")
    if data["terrain"]:
        extras.append(f"on {pretty(data['terrain'])}")
    if data["aurora_veil"] or (data["reflect"] and physical) or (data["light_screen"] and not physical):
        extras.append("through " + ("Aurora Veil" if data["aurora_veil"] else "Reflect" if physical else "Light Screen"))
    if result["critical_hit"]:
        extras.append("on a critical hit")
    summary = " ".join(part for part in attacker + ["vs."] + defender + extras if part)
    return f"{summary}: {result['min_damage']}-{result['max_damage']} ({result['min_percent']:g} - {result['max_percent']:g}%) -- {result['ko_chance']}"


# The EVs in a stat with a + or - for the nature, like "252+ Atk".
def ev_text(build, stat):
    if stat is None:
        return ""
    raised, lowered = NATURES[build["nature"]]
    sign = "+" if stat == raised else "-" if stat == lowered else ""
    return f"{build['evs'][stat]}{sign} {STAT_LABELS[stat]}"


# A side's stat stage in a stat, like "+2". Blank if it's 0.
def boost_text(data, side, stat):
    key = next((key for key, name in STAT_KEYS.items() if name == stat), None)
    if key is None:
        return ""
    stage = data.get(f"{side}_boost_{key}") or 0
    return f"{stage:+d}" if stage else ""


# "Lvl 30" for any level that isn't the default 50.
def level_text(build):
    return "" if build["level"] == DEFAULT_LEVEL else f"Lvl {build['level']}"


# How much of the defender's HP bar is left after the lowest and highest rolls, as % widths for the HP bar.
def hp_bar_widths(result):
    current = result["defender_hp"] * 100 / result["defender_max_hp"]
    after_min = max(0.0, current - result["min_percent"])
    after_max = max(0.0, current - result["max_percent"])
    color = "green" if after_min > 50 else "yellow" if after_min > 20 else "red"
    return {"current": round(current, 1), "after_min": round(after_min, 1), "after_max": round(after_max, 1), "range": round(after_min - after_max, 1), "color": color}


# "Neutral" for 1x, otherwise the multiplier, like "2x effective".
def effectiveness_text(effectiveness):
    return "Neutral" if effectiveness == 1 else f"{effectiveness:g}x effective"