from django import forms
from .damage_calculator import DamageCalculator
from .models import Moves, Species
from .stats import DEFAULT_LEVEL, DEFAULT_NATURE, MAX_EV, MAX_IV, MAX_LEVEL, MIN_LEVEL, NATURES, make_build

# Damage Calculator Form

SIDES = ("attacker", "defender")

# Short names for each stat. The form's field names use them (attacker_ev_atk, defender_iv_spe, etc.).
STAT_KEYS = {"hp": "hp", "atk": "attack", "def": "defense", "spa": "special-attack", "spd": "special-defense", "spe": "speed"}
STAT_LABELS = {"hp": "HP", "attack": "Atk", "defense": "Def", "special-attack": "SpA", "special-defense": "SpD", "speed": "Spe"}

# The 218 held items the damage calculator handles.
ITEMS = [
    "ability-shield", "absorb-bulb", "adamant-crystal", "adamant-orb", "adrenaline-orb", "aguav-berry", "air-balloon", "apicot-berry", "aspear-berry",
    "assault-vest", "babiri-berry", "berry-juice", "big-root", "black-belt", "black-glasses", "black-sludge", "blue-orb", "booster-energy",
    "bright-powder", "bug-gem", "bug-memory", "burn-drive", "cell-battery", "charcoal", "charti-berry", "cheri-berry", "chesto-berry", "chilan-berry",
    "chill-drive", "choice-band", "choice-scarf", "choice-specs", "chople-berry", "clear-amulet", "coba-berry", "colbur-berry", "cornerstone-mask",
    "covert-cloak", "custap-berry", "dark-gem", "dark-memory", "deep-sea-scale", "deep-sea-tooth", "destiny-knot", "douse-drive", "draco-plate",
    "dragon-fang", "dragon-gem", "dragon-memory", "dread-plate", "earth-plate", "eject-button", "eject-pack", "electric-gem", "electric-memory",
    "electric-seed", "enigma-berry", "eviolite", "expert-belt", "fairy-feather", "fairy-gem", "fairy-memory", "fighting-gem", "fighting-memory",
    "figy-berry", "fire-gem", "fire-memory", "fist-plate", "flame-orb", "flame-plate", "float-stone", "flying-gem", "flying-memory", "focus-band",
    "focus-sash", "full-incense", "ganlon-berry", "ghost-gem", "ghost-memory", "grass-gem", "grass-memory", "grassy-seed", "griseous-core",
    "griseous-orb", "ground-gem", "ground-memory", "haban-berry", "hard-stone", "hearthflame-mask", "iapapa-berry", "ice-gem", "ice-memory",
    "icicle-plate", "insect-plate", "iron-ball", "iron-plate", "jaboca-berry", "kasib-berry", "kebia-berry", "kee-berry", "kings-rock", "lagging-tail",
    "lansat-berry", "lax-incense", "leek", "leftovers", "leppa-berry", "liechi-berry", "life-orb", "light-ball", "loaded-dice", "lucky-punch",
    "lum-berry", "luminous-moss", "lustrous-globe", "lustrous-orb", "macho-brace", "magnet", "mago-berry", "maranga-berry", "meadow-plate", "mental-herb",
    "metal-coat", "metal-powder", "metronome", "micle-berry", "mind-plate", "miracle-seed", "mirror-herb", "misty-seed", "muscle-band", "mystic-water",
    "never-melt-ice", "normal-gem", "occa-berry", "odd-incense", "oran-berry", "passho-berry", "payapa-berry", "pecha-berry", "persim-berry",
    "petaya-berry", "pixie-plate", "poison-barb", "poison-gem", "poison-memory", "power-anklet", "power-band", "power-belt", "power-bracer", "power-herb",
    "power-lens", "power-weight", "psychic-gem", "psychic-memory", "psychic-seed", "punching-glove", "quick-claw", "quick-powder", "rawst-berry",
    "razor-claw", "razor-fang", "red-card", "red-orb", "rindo-berry", "ring-target", "rock-gem", "rock-incense", "rock-memory", "rocky-helmet",
    "room-service", "rose-incense", "roseli-berry", "rowap-berry", "safety-goggles", "salac-berry", "scope-lens", "sea-incense", "sharp-beak",
    "shed-shell", "shell-bell", "shock-drive", "shuca-berry", "silk-scarf", "silver-powder", "sitrus-berry", "sky-plate", "snowball", "soft-sand",
    "soul-dew", "spell-tag", "splash-plate", "spooky-plate", "starf-berry", "steel-gem", "steel-memory", "sticky-barb", "stone-plate", "tanga-berry",
    "thick-club", "throat-spray", "toxic-orb", "toxic-plate", "twisted-spoon", "utility-umbrella", "wacan-berry", "water-gem", "water-memory",
    "wave-incense", "weakness-policy", "wellspring-mask", "white-herb", "wide-lens", "wiki-berry", "wise-glasses", "yache-berry", "zap-plate", "zoom-lens"
]

STATUS_CHOICES = [("", "Healthy"), ("burn", "Burned"), ("paralysis", "Paralyzed"), ("poison", "Poisoned"), ("badly-poison", "Badly poisoned"), ("sleep", "Asleep"), ("freeze", "Frozen")]
WEATHER_CHOICES = [("", "None"), ("sun", "Sun"), ("rain", "Rain"), ("sandstorm", "Sandstorm"), ("snow", "Snow"), ("extreme-sun", "Extreme sun"), ("heavy-rain", "Heavy rain"), ("strong-winds", "Strong winds")]
TERRAIN_CHOICES = [("", "None"), ("electric-terrain", "Electric"), ("grassy-terrain", "Grassy"), ("psychic-terrain", "Psychic"), ("misty-terrain", "Misty")]
BOOST_CHOICES = [(stage, f"{stage:+d}" if stage else "0") for stage in range(6, -7, -1)]

# The matchup the page shows before anything is picked.
DEFAULT_MATCHUP = {"attacker_pokemon": "garchomp", "defender_pokemon": "snorlax", "move": "earthquake"}


def pretty(name):
    return name.replace("-", " ").title() if name else ""


# Labels a nature with the stats it raises and lowers, like "Adamant (+Atk, -SpA)".
def nature_label(nature):
    raised, lowered = NATURES[nature]
    if raised is None:
        return pretty(nature)
    return f"{pretty(nature)} (+{STAT_LABELS[raised]}, -{STAT_LABELS[lowered]})"


NATURE_CHOICES = [(nature, nature_label(nature)) for nature in sorted(NATURES)]


# Every input on the calc page. Each side gets a Pokémon, ability, item, level, nature, status, HP %, IVs, EVs and stat stages.
class DamageCalcForm(forms.Form):
    move = forms.CharField()
    weather = forms.ChoiceField(choices=WEATHER_CHOICES, required=False)
    terrain = forms.ChoiceField(choices=TERRAIN_CHOICES, required=False)
    critical_hit = forms.BooleanField(required=False)
    reflect = forms.BooleanField(required=False)
    light_screen = forms.BooleanField(required=False)
    aurora_veil = forms.BooleanField(required=False)

    # Adds the same fields for both sides.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        number = {"class": "num", "inputmode": "numeric"}
        for side in SIDES:
            name = side.title()
            self.fields[f"{side}_pokemon"] = forms.CharField(label=f"{name} Pokémon", widget=forms.TextInput(attrs={"list": "pokemon-names", "autocomplete": "off", "spellcheck": "false"}))
            self.fields[f"{side}_ability"] = forms.CharField(label=f"{name} ability", required=False)
            self.fields[f"{side}_item"] = forms.CharField(label=f"{name} item", required=False, widget=forms.TextInput(attrs={"list": "item-names", "autocomplete": "off", "spellcheck": "false", "placeholder": "No item"}))
            self.fields[f"{side}_level"] = forms.IntegerField(label=f"{name} level", min_value=MIN_LEVEL, max_value=MAX_LEVEL, initial=DEFAULT_LEVEL, required=False, widget=forms.NumberInput(attrs=number))
            self.fields[f"{side}_nature"] = forms.ChoiceField(label=f"{name} nature", choices=NATURE_CHOICES, initial=DEFAULT_NATURE, required=False)
            self.fields[f"{side}_status"] = forms.ChoiceField(label=f"{name} status", choices=STATUS_CHOICES, required=False)
            self.fields[f"{side}_hp_percent"] = forms.IntegerField(label=f"{name} HP %", min_value=1, max_value=100, initial=100, required=False, widget=forms.NumberInput(attrs=number))
            for key, stat in STAT_KEYS.items():
                label = f"{name} {STAT_LABELS[stat]}"
                self.fields[f"{side}_iv_{key}"] = forms.IntegerField(label=f"{label} IVs", min_value=0, max_value=MAX_IV, initial=MAX_IV, required=False, widget=forms.NumberInput(attrs=number))
                self.fields[f"{side}_ev_{key}"] = forms.IntegerField(label=f"{label} EVs", min_value=0, max_value=MAX_EV, initial=0, required=False, widget=forms.NumberInput(attrs=number))
                if key != "hp":
                    self.fields[f"{side}_boost_{key}"] = forms.TypedChoiceField(label=f"{label} stage", choices=BOOST_CHOICES, coerce=int, initial=0, empty_value=0, required=False)

    # The starting values for every field, with the default matchup filled in.
    @classmethod
    def default_data(cls):
        defaults = {name: field.initial for name, field in cls().fields.items() if field.initial is not None}
        return {**defaults, **DEFAULT_MATCHUP}

    # Checks the Pokémon, move, abilities, items and builds. Adds the Species, Moves and full builds to cleaned_data.
    def clean(self):
        data = super().clean()
        for side in SIDES:
            name = (data.get(f"{side}_pokemon") or "").strip().lower()
            species = Species.objects.filter(name=name).first()
            if species is None:
                if name:
                    self.add_error(f"{side}_pokemon", f"{name} isn't a Pokémon.")
                continue
            data[f"{side}_species"] = species
            ability = data.get(f"{side}_ability")
            if ability and ability not in species.abilities:
                self.add_error(f"{side}_ability", f"{pretty(species.name)} can't have {pretty(ability)}.")
            item = data.get(f"{side}_item")
            if item and item not in ITEMS:
                self.add_error(f"{side}_item", f"{item} isn't an item the calc knows.")
            try:
                data[f"{side}_build"] = make_build(self.build(side, data))
            except ValueError as error:
                self.add_error(None, f"{side.title()}: {error}")
        move = Moves.objects.filter(name=(data.get("move") or "").strip().lower()).first()
        if move is None or move.damage_class == "status":
            self.add_error("move", "Pick a move that does damage.")
        else:
            data["move_data"] = move
        return data

    # Every error with the field it's for, like "Attacker Spe EVs: Ensure this value is less than or equal to 252."
    def error_list(self):
        messages = []
        for name, errors in self.errors.items():
            label = self.fields[name].label if name in self.fields else None
            messages += [f"{label}: {error}" if label else error for error in errors]
        return messages

    # One side's level, nature, EVs and IVs, in the format make_build takes. Blank fields use the defaults.
    def build(self, side, data):
        evs = {stat: data.get(f"{side}_ev_{key}") or 0 for key, stat in STAT_KEYS.items()}
        ivs = {stat: MAX_IV if data.get(f"{side}_iv_{key}") is None else data[f"{side}_iv_{key}"] for key, stat in STAT_KEYS.items()}
        return {"level": data.get(f"{side}_level") or DEFAULT_LEVEL, "nature": data.get(f"{side}_nature") or DEFAULT_NATURE, "evs": evs, "ivs": ivs}

    # One side's stat stages, like {"attack": 2}. Only stages that aren't 0 are kept.
    def boosts(self, side):
        stages = {stat: self.cleaned_data.get(f"{side}_boost_{key}") or 0 for key, stat in STAT_KEYS.items() if key != "hp"}
        return {stat: stage for stat, stage in stages.items() if stage}

    # Builds the DamageCalculator from the form. Only call this after is_valid().
    def make_calculator(self):
        data = self.cleaned_data
        move = data["move_data"]
        return DamageCalculator(data["attacker_species"], data["defender_species"], move.type, move.power, move.name,
                                data["attacker_status"] or None, data["defender_status"] or None, data["weather"] or None, data["terrain"] or None,
                                data["attacker_ability"] or None, data["defender_ability"] or None, data["attacker_item"] or None, data["defender_item"] or None,
                                data["attacker_build"], data["defender_build"])

    # The options calculate() takes: crit, stat stages, current HP and screens.
    def calc_options(self, calc):
        data = self.cleaned_data
        return {"critical_hit": data["critical_hit"], "attacker_boosts": self.boosts("attacker"), "defender_boosts": self.boosts("defender"),
                "attacker_hp": current_hp(calc.attacker_stats["hp"], data["attacker_hp_percent"]), "defender_hp": current_hp(calc.defender_stats["hp"], data["defender_hp_percent"]),
                "reflect": data["reflect"], "light_screen": data["light_screen"], "aurora_veil": data["aurora_veil"]}


# Turns an HP % into HP. Always at least 1, and full HP if left blank.
def current_hp(max_hp, percent):
    if percent is None:
        return max_hp
    return max(1, max_hp * percent // 100)



# Team Builder Form

TEAM_SIZE = 6


# The six slots on the team builder page. Each slot takes a Pokémon's name.
class TeamForm(forms.Form):
    # Adds a text box for each slot.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for slot in range(1, TEAM_SIZE + 1):
            self.fields[f"pokemon_{slot}"] = forms.CharField(label=f"Pokémon {slot}", required=False, widget=forms.TextInput(attrs={"list": "pokemon-names", "autocomplete": "off", "spellcheck": "false", "placeholder": "Pick a Pokémon"}))

    # Looks up each slot's Pokémon
    def clean(self):
        data = super().clean()
        team = []
        for slot in range(1, TEAM_SIZE + 1):
            name = (data.get(f"pokemon_{slot}") or "").strip().lower()
            species = Species.objects.filter(name=name).first() if name else None
            if name and species is None:
                self.add_error(f"pokemon_{slot}", f"{name} isn't a Pokémon.")
            elif species is not None and species in team:
                self.add_error(f"pokemon_{slot}", f"{pretty(species.name)} is already on your team.")
                species = None
            team.append(species)
        data["team"] = team
        return data