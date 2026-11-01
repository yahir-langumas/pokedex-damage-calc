import importlib
import re
from unittest.mock import Mock, patch
import requests
from django.core.management import call_command
from django.test import TestCase
from django.urls import clear_url_caches, reverse
import config.urls
from pokedata.models import Learnset, Moves, Species, Type
from pokedata.damage_calculator import DamageCalculator, apply_stage, chain_modifiers, damage_rolls, ko_chance_text, modifier_from, poke_round, to_4096
from pokedata.stats import calculate_stats, make_build
from pokedata.forms import ITEMS, pretty, slug

# Builds a DamageCalculator from Pokémon and move names.
def make_calc(attacker, defender, move, attacker_ability=None, defender_ability=None, attacker_item=None, defender_item=None, weather=None, attacker_build=None, defender_build=None):
    move_data = Moves.objects.get(name=move)
    return DamageCalculator(Species.objects.get(name=attacker), Species.objects.get(name=defender), move_data.type, move_data.power, move_data.name, None, None, weather, None, attacker_ability, defender_ability, attacker_item, defender_item, attacker_build, defender_build)
 
 
class TypeAndWeatherTests(TestCase):
    fixtures = ["pokedata"]
 
    # Fire is super effective on Grass.
    def test_super_effective(self):
        self.assertEqual(make_calc("charizard", "venusaur", "flamethrower").type_effectiveness_defender(), 2)
 
    # Normal moves can't hit Ghost types.
    def test_immune(self):
        self.assertEqual(make_calc("snorlax", "gengar", "tackle").type_effectiveness_defender(), 0)
 
    # Rain boosts Water moves by 50%.
    def test_rain_boosts_water(self):
        self.assertEqual(make_calc("blastoise", "snorlax", "surf", weather="rain").weather_move_power(), 135)

    # Sun boosts Fire moves and weakens Water moves by 50%.
    def test_sun(self):
        self.assertEqual(make_calc("charizard", "snorlax", "flamethrower", weather="sun").weather_move_power(), 135)
        self.assertEqual(make_calc("blastoise", "snorlax", "surf", weather="sun").weather_move_power(), 45)

    # Rain weakens Fire moves.
    def test_rain_weakens_fire(self):
        self.assertEqual(make_calc("charizard", "snorlax", "flamethrower", weather="rain").weather_move_power(), 45)

    # Weather doesn't change moves of other types.
    def test_weather_other_types(self):
        self.assertEqual(make_calc("machamp", "snorlax", "close-combat", weather="sun").weather_move_power(), 120)

    # Ground is super effective on both of Heatran's types, so it's 4x.
    def test_double_weakness(self):
        self.assertEqual(make_calc("garchomp", "heatran", "earthquake").type_effectiveness_defender(), 4)

    # Water resists Fire.
    def test_not_very_effective(self):
        self.assertEqual(make_calc("charizard", "blastoise", "flamethrower").type_effectiveness_defender(), 0.5)
 
 
class AbilityTests(TestCase):
    fixtures = ["pokedata"]
 
    # Levitate makes Ground moves miss.
    def test_levitate(self):
        self.assertEqual(make_calc("garchomp", "rotom", "earthquake", defender_ability="levitate").levitate_ability_defender(), 0)
 
    # Mold Breaker ignores Levitate.
    def test_mold_breaker_ignores_levitate(self):
        self.assertEqual(make_calc("excadrill", "rotom", "earthquake", "mold-breaker", "levitate").mold_breaker_ability_attacker(), [])
 
    # Huge Power doubles Attack.
    def test_huge_power(self):
        calc = make_calc("azumarill", "snorlax", "aqua-tail", attacker_ability="huge-power")
        self.assertEqual(calc.huge_power_ability_attacker(), calc.attacker_stats["attack"] * 2)
 
    # Thick Fat halves Fire damage.
    def test_thick_fat(self):
        self.assertEqual(make_calc("charizard", "snorlax", "flamethrower", defender_ability="thick-fat").thick_fat_ability_defender(), 45)
 
    # Technician boosts moves with 60 power or less.
    def test_technician(self):
        self.assertEqual(make_calc("scizor", "snorlax", "bullet-punch", attacker_ability="technician").technician_ability_attacker(), 60)

    # Adaptability makes STAB 2x instead of 1.5x, and does nothing for moves that aren't STAB.
    def test_adaptability(self):
        self.assertEqual(make_calc("porygon-z", "snorlax", "hyper-beam", attacker_ability="adaptability").adaptability_ability_attacker(), 2)
        self.assertEqual(make_calc("porygon-z", "snorlax", "hyper-beam", attacker_ability="download").adaptability_ability_attacker(), 1.5)
        self.assertEqual(make_calc("porygon-z", "snorlax", "shadow-ball", attacker_ability="adaptability").adaptability_ability_attacker(), 1)

    # Overgrow boosts Grass moves by 1.5x at 1/3 HP or less.
    def test_overgrow(self):
        calc = make_calc("venusaur", "snorlax", "energy-ball", attacker_ability="overgrow")
        self.assertEqual(calc.overgrow_ability_attacker(50, 155), 135)
        self.assertEqual(calc.overgrow_ability_attacker(155, 155), 90)

    # Fur Coat doubles Defense.
    def test_fur_coat(self):
        calc = make_calc("machamp", "furfrou", "close-combat", defender_ability="fur-coat")
        self.assertEqual(calc.fur_coat_ability_defender(), calc.defender_stats["defense"] * 2)

    # Reckless boosts recoil moves by 1.2x.
    def test_reckless(self):
        self.assertEqual(make_calc("staraptor", "snorlax", "double-edge", attacker_ability="reckless").reckless_ability_attacker(), 144)
        self.assertEqual(make_calc("staraptor", "snorlax", "tackle", attacker_ability="reckless").reckless_ability_attacker(), 40)

    # Mega Launcher boosts pulse moves and Sharpness boosts slicing moves by 1.5x.
    def test_mega_launcher_and_sharpness(self):
        self.assertEqual(make_calc("blastoise", "snorlax", "aura-sphere", attacker_ability="mega-launcher").mega_launcher_ability_attacker(), 120)
        self.assertEqual(make_calc("gallade", "snorlax", "leaf-blade", attacker_ability="sharpness").sharpness_ability_attacker(), 135)

    # Shadow Shield halves dmg at full HP.
    def test_shadow_shield(self):
        calc = make_calc("garchomp", "lunala", "earthquake", defender_ability="shadow-shield")
        self.assertEqual(calc.shadow_shield_ability_defender(100, 100), 50)
        self.assertEqual(calc.shadow_shield_ability_defender(99, 100), 100)
        
class ItemTests(TestCase):
    fixtures = ["pokedata"]
 
    # Oran Berry heals 10 HP at half HP or less.
    def test_oran_berry(self):
        self.assertEqual(make_calc("snorlax", "machamp", "tackle", attacker_item="oran-berry").oran_berry_item_attacker(40, 100), 10)
 
    # Klutz stops the Pokémon from using its item.
    def test_klutz(self):
        self.assertIsNone(make_calc("lopunny", "machamp", "tackle", attacker_ability="klutz", attacker_item="oran-berry").held_item_attacker())
 
    # Unnerve stops the other Pokémon from eating Berries.
    def test_unnerve(self):
        self.assertIsNone(make_calc("snorlax", "mewtwo", "tackle", defender_ability="unnerve", attacker_item="oran-berry").held_berry_attacker())

    # Life Orb boosts dmg by 1.3x and costs 10% HP, but Magic Guard stops the HP loss.
    def test_life_orb(self):
        self.assertEqual(make_calc("garchomp", "snorlax", "earthquake", attacker_item="life-orb").life_orb_item_attacker(), (1.3, 0.1))
        self.assertEqual(make_calc("clefable", "snorlax", "moonblast", attacker_ability="magic-guard", attacker_item="life-orb").life_orb_item_attacker(), (1.3, 0))

    # Choice Specs boosts Sp. Atk by 1.5x.
    def test_choice_specs(self):
        calc = make_calc("latios", "snorlax", "draco-meteor", attacker_item="choice-specs")
        self.assertEqual(calc.choice_specs_item_attacker(), calc.attacker_stats["special-attack"] * 1.5)

    # Charcoal boosts Fire moves by 1.2x but not other types.
    def test_type_boost_item(self):
        self.assertEqual(make_calc("charizard", "snorlax", "flamethrower", attacker_item="charcoal").type_boost_item_attacker(), 108)
        self.assertEqual(make_calc("charizard", "snorlax", "air-slash", attacker_item="charcoal").type_boost_item_attacker(), 75)

    # Fire Gem boosts Fire moves by 1.3x.
    def test_gem(self):
        self.assertEqual(make_calc("charizard", "snorlax", "flamethrower", attacker_item="fire-gem").gem_item_attacker(), 117)

    # Light Ball only works on Pikachu.
    def test_light_ball(self):
        pikachu = make_calc("pikachu", "snorlax", "thunderbolt", attacker_item="light-ball")
        self.assertEqual(pikachu.light_ball_item_attacker(), (pikachu.attacker_stats["attack"] * 2, pikachu.attacker_stats["special-attack"] * 2))
        raichu = make_calc("raichu", "snorlax", "thunderbolt", attacker_item="light-ball")
        self.assertEqual(raichu.light_ball_item_attacker(), (raichu.attacker_stats["attack"], raichu.attacker_stats["special-attack"]))

    # Metal Powder doubles Ditto's Defense.
    def test_metal_powder(self):
        calc = make_calc("machamp", "ditto", "close-combat", defender_item="metal-powder")
        self.assertEqual(calc.metal_powder_item_defender(), calc.defender_stats["defense"] * 2)

    
class StatTests(TestCase):
    fixtures = ["pokedata"]
 
    # With no build, a Pokémon is level 50 with 0 EVs, 31 IVs and a neutral nature.
    def test_default_build(self):
        machamp = Species.objects.get(name="machamp")
        self.assertEqual(calculate_stats(machamp.stats), {"hp": 165, "attack": 150, "defense": 100, "special-attack": 85, "special-defense": 105, "speed": 75})
 
    # 4 HP / 252 Atk / 252 Spe Jolly Garchomp. Matches Smogon's calc.
    def test_evs_and_nature(self):
        garchomp = Species.objects.get(name="garchomp")
        stats = calculate_stats(garchomp.stats, {"evs": {"hp": 4, "attack": 252, "speed": 252}, "nature": "jolly"})
        self.assertEqual((stats["hp"], stats["attack"], stats["special-attack"], stats["speed"]), (184, 182, 90, 169))
 
    # Lower levels mean lower stats.
    def test_level(self):
        garchomp = Species.objects.get(name="garchomp")
        self.assertEqual(calculate_stats(garchomp.stats, {"level": 1})["hp"], 13)
 
    # Shedinja always has 1 HP.
    def test_shedinja(self):
        shedinja = Species.objects.get(name="shedinja")
        self.assertEqual(calculate_stats(shedinja.stats, pokedex_id=shedinja.pokedex_id)["hp"], 1)
 
    # Levels over 50, more than 510 EVs and IVs over 31 aren't allowed.
    def test_bad_builds(self):
        for build in ({"level": 51}, {"evs": {"attack": 252, "speed": 252, "hp": 8}}, {"ivs": {"speed": 32}}):
            with self.assertRaises(ValueError):
                make_build(build)
 
    # The calculator uses real stats, so Choice Band boosts Machamp's level 50 Attack (150).
    def test_calculator_uses_real_stats(self):
        self.assertEqual(make_calc("machamp", "snorlax", "close-combat", attacker_item="choice-band").choice_band_item_attacker(), 225)

    # Adamant raises Attack by 10% and lowers Sp. Atk by 10%.
    def test_nature_raises_and_lowers(self):
        garchomp = Species.objects.get(name="garchomp")
        stats = calculate_stats(garchomp.stats, {"nature": "adamant"})
        self.assertEqual((stats["attack"], stats["special-attack"]), (165, 90))

    # All 5 neutral natures give the same stats.
    def test_neutral_natures(self):
        garchomp = Species.objects.get(name="garchomp")
        stats = calculate_stats(garchomp.stats)
        for nature in ("hardy", "docile", "serious", "bashful", "quirky"):
            self.assertEqual(calculate_stats(garchomp.stats, {"nature": nature}), stats)

    # make_build fills in level 50, 0 EVs, 31 IVs and a neutral nature.
    def test_make_build_defaults(self):
        build = make_build()
        self.assertEqual((build["level"], build["nature"]), (50, "serious"))
        self.assertEqual(set(build["evs"].values()), {0})
        self.assertEqual(set(build["ivs"].values()), {31})

    # Natures that don't exist aren't allowed.
    def test_bad_nature(self):
        with self.assertRaises(ValueError):
            make_build({"nature": "spicy"})

    # Level 0 and levels that aren't whole numbers aren't allowed.
    def test_bad_level_values(self):
        for level in (0, "50", 25.5):
            with self.assertRaises(ValueError):
                make_build({"level": level})

    # More than 252 EVs in one stat and negative IVs aren't allowed.
    def test_bad_ev_and_iv_values(self):
        with self.assertRaises(ValueError):
            make_build({"evs": {"attack": 253}})
        with self.assertRaises(ValueError):
            make_build({"ivs": {"hp": -1}})

    # Exactly 510 EVs is allowed.
    def test_max_evs_allowed(self):
        build = make_build({"evs": {"attack": 252, "speed": 252, "hp": 6}})
        self.assertEqual(sum(build["evs"].values()), 510)

    # 252 HP EVs gives Garchomp 215 HP.
    def test_hp_evs(self):
        garchomp = Species.objects.get(name="garchomp")
        self.assertEqual(calculate_stats(garchomp.stats, {"evs": {"hp": 252}})["hp"], 215)

    # 0 Speed IVs lowers Garchomp's Speed from 122 to 107.
    def test_zero_ivs(self):
        garchomp = Species.objects.get(name="garchomp")
        self.assertEqual(calculate_stats(garchomp.stats, {"ivs": {"speed": 0}})["speed"], 107)

# Tests for the calc's helper functions: Speed, Protosynthesis/Quark Drive, weight, battle forms, ignored abilities and move type.
class SpeedTests(TestCase):
    fixtures = ["pokedata"]

    # A level 50 Garchomp with 0 Speed EVs has 122 Speed.
    def test_base_speed(self):
        self.assertEqual(make_calc("garchomp", "snorlax", "earthquake").final_speed_attacker(), 122)

    # +1 Speed is 1.5x.
    def test_speed_stage(self):
        self.assertEqual(make_calc("garchomp", "snorlax", "earthquake").final_speed_attacker({"speed": 1}), 183)

    # Choice Scarf is 1.5x and Iron Ball is 0.5x.
    def test_speed_items(self):
        self.assertEqual(make_calc("garchomp", "snorlax", "earthquake", attacker_item="choice-scarf").final_speed_attacker(), 183)
        self.assertEqual(make_calc("garchomp", "snorlax", "earthquake", attacker_item="iron-ball").final_speed_attacker(), 61)

    # Paralysis halves Speed, unless the Pokémon has Quick Feet (which boosts it 1.5x instead).
    def test_paralysis(self):
        species = Species.objects.get(name="garchomp")
        paralyzed = DamageCalculator(species, Species.objects.get(name="snorlax"), "ground", 100, "earthquake", "paralysis", None)
        quick_feet = DamageCalculator(species, Species.objects.get(name="snorlax"), "ground", 100, "earthquake", "paralysis", None, attacker_ability="quick-feet")
        self.assertEqual(paralyzed.final_speed_attacker(), 61)
        self.assertEqual(quick_feet.final_speed_attacker(), 183)

    # Swift Swim doubles Speed in rain.
    def test_swift_swim(self):
        self.assertEqual(make_calc("kingdra", "snorlax", "surf", attacker_ability="swift-swim", weather="rain").final_speed_attacker(), 210)

    # The defender version: Chlorophyll doubles Speed in sun.
    def test_defender_speed(self):
        self.assertEqual(make_calc("snorlax", "venusaur", "tackle", defender_ability="chlorophyll", weather="sun").final_speed_defender(), 200)

    # Quark Drive with Booster Energy boosts Iron Bundle's Speed (its highest stat) by 1.5x.
    def test_quark_drive_speed(self):
        self.assertEqual(make_calc("iron-bundle", "snorlax", "hydro-pump", attacker_ability="quark-drive", attacker_item="booster-energy").final_speed_attacker(), 234)

    # -1 Speed is 2/3.
    def test_speed_drop(self):
        self.assertEqual(make_calc("garchomp", "snorlax", "earthquake").final_speed_attacker({"speed": -1}), 81)

    # Sand Rush doubles Speed in sandstorm.
    def test_sand_rush(self):
        self.assertEqual(make_calc("excadrill", "snorlax", "earthquake", attacker_ability="sand-rush", weather="sandstorm").final_speed_attacker(), 216)

    # Slush Rush doubles Speed in snow.
    def test_slush_rush(self):
        self.assertEqual(make_calc("cetitan", "snorlax", "icicle-crash", attacker_ability="slush-rush", weather="snow").final_speed_attacker(), 186)


class ParadoxStatTests(TestCase):
    fixtures = ["pokedata"]

    # Without Protosynthesis or Quark Drive, nothing gets boosted.
    def test_not_active(self):
        self.assertIsNone(make_calc("garchomp", "snorlax", "earthquake").paradox_boosted_stat_attacker())

    # Quark Drive only works with Booster Energy or Electric Terrain.
    def test_needs_booster_or_terrain(self):
        self.assertIsNone(make_calc("iron-bundle", "snorlax", "hydro-pump", attacker_ability="quark-drive").paradox_boosted_stat_attacker())

    # Iron Bundle's highest stat is Speed.
    def test_highest_stat(self):
        self.assertEqual(make_calc("iron-bundle", "snorlax", "hydro-pump", attacker_ability="quark-drive", attacker_item="booster-energy").paradox_boosted_stat_attacker(), "speed")

    # Stat stages count, so +2 Sp. Atk makes Sp. Atk the highest instead.
    def test_stat_stages_count(self):
        calc = make_calc("iron-bundle", "snorlax", "hydro-pump", attacker_ability="quark-drive", attacker_item="booster-energy")
        self.assertEqual(calc.paradox_boosted_stat_attacker({"special-attack": 2}), "special-attack")

    # Great Tusk's Attack and Defense tie, and Attack wins ties.
    def test_ties_go_to_attack(self):
        self.assertEqual(make_calc("great-tusk", "snorlax", "headlong-rush", attacker_ability="protosynthesis", weather="sun").paradox_boosted_stat_attacker(), "attack")

    # The defender version.
    def test_defender(self):
        self.assertEqual(make_calc("snorlax", "iron-bundle", "tackle", defender_ability="quark-drive", defender_item="booster-energy").paradox_boosted_stat_defender(), "speed")


class WeightTests(TestCase):
    fixtures = ["pokedata"]

    # Weight is in hectograms. Snorlax is 460 kg.
    def test_base_weight(self):
        self.assertEqual(make_calc("snorlax", "machamp", "heavy-slam").final_weight_attacker(), 4600)

    # Heavy Metal doubles weight, Light Metal and Float Stone halve it.
    def test_weight_changes(self):
        self.assertEqual(make_calc("snorlax", "machamp", "heavy-slam", attacker_ability="heavy-metal").final_weight_attacker(), 9200)
        self.assertEqual(make_calc("snorlax", "machamp", "heavy-slam", attacker_ability="light-metal").final_weight_attacker(), 2300)
        self.assertEqual(make_calc("snorlax", "machamp", "heavy-slam", attacker_item="float-stone").final_weight_attacker(), 2300)

    # Light Metal and Float Stone together quarter it.
    def test_light_metal_and_float_stone(self):
        self.assertEqual(make_calc("snorlax", "machamp", "heavy-slam", attacker_ability="light-metal", attacker_item="float-stone").final_weight_attacker(), 1150)

    # The defender version.
    def test_defender_weight(self):
        self.assertEqual(make_calc("machamp", "snorlax", "low-kick", defender_ability="heavy-metal").final_weight_defender(), 9200)

    # Float Stone on the defender halves its weight.
    def test_defender_float_stone(self):
        self.assertEqual(make_calc("machamp", "snorlax", "low-kick", defender_item="float-stone").final_weight_defender(), 2300)


class BattleFormTests(TestCase):
    fixtures = ["pokedata"]

    # change_form swaps in the new form and recalculates its stats, without changing the Pokémon from the database.
    def test_change_form(self):
        calc = make_calc("groudon", "snorlax", "earthquake")
        original = calc.attacker
        primal_stats = {"hp": 100, "attack": 180, "defense": 160, "special-attack": 150, "special-defense": 90, "speed": 90}
        calc.change_form("attacker", "groudon-primal", ["ground", "fire"], primal_stats, "desolate-land")
        self.assertEqual((calc.attacker.name, calc.attacker.types, calc.attacker_abilities), ("groudon-primal", ["ground", "fire"], ["desolate-land"]))
        self.assertEqual(calc.attacker_stats["attack"], 200)
        self.assertEqual(original.name, "groudon")

    # Red Orb turns Groudon into Primal Groudon.
    def test_red_orb(self):
        calc = make_calc("groudon", "snorlax", "earthquake", attacker_item="red-orb")
        calc.use_battle_forms()
        self.assertEqual((calc.attacker.name, calc.attacker.types, calc.attacker_abilities), ("groudon-primal", ["ground", "fire"], ["desolate-land"]))

    # Blue Orb turns a defending Kyogre into Primal Kyogre.
    def test_blue_orb_defender(self):
        calc = make_calc("snorlax", "kyogre", "tackle", defender_item="blue-orb")
        calc.use_battle_forms()
        self.assertEqual((calc.defender.name, calc.defender_abilities), ("kyogre-primal", ["primordial-sea"]))

    # Aegislash goes into Blade Forme to attack, but King's Shield keeps it in Shield Forme.
    def test_stance_change(self):
        attacking = make_calc("aegislash-shield", "machamp", "shadow-ball")
        attacking.use_battle_forms()
        self.assertEqual(attacking.attacker.name, "aegislash-blade")
        self.assertEqual(attacking.attacker_stats["attack"], 160)
        shielding = make_calc("aegislash-shield", "machamp", "kings-shield")
        shielding.use_battle_forms()
        self.assertEqual(shielding.attacker.name, "aegislash-shield")

    # Castform turns Water type in rain.
    def test_castform(self):
        calc = make_calc("castform", "snorlax", "weather-ball", weather="rain")
        calc.use_battle_forms()
        self.assertEqual((calc.attacker.name, calc.attacker.types), ("castform-rainy", ["water"]))

    # Arceus turns Fire type with a Flame Plate.
    def test_arceus(self):
        calc = make_calc("arceus", "snorlax", "judgment", attacker_item="flame-plate")
        calc.use_battle_forms()
        self.assertEqual((calc.attacker.name, calc.attacker.types), ("arceus-fire", ["fire"]))

    # Ogerpon's Wellspring Mask makes it Grass/Water with Water Absorb.
    def test_ogerpon(self):
        calc = make_calc("ogerpon", "snorlax", "ivy-cudgel", attacker_item="wellspring-mask")
        calc.use_battle_forms()
        self.assertEqual((calc.attacker.name, calc.attacker.types, calc.attacker_abilities), ("ogerpon-wellspring-mask", ["grass", "water"], ["water-absorb"]))

    # Nothing changes for a Pokémon without a form-changing item or ability.
    def test_no_form(self):
        calc = make_calc("garchomp", "snorlax", "earthquake")
        calc.use_battle_forms()
        self.assertEqual(calc.attacker.name, "garchomp")


class IgnoreAbilityTests(TestCase):
    fixtures = ["pokedata"]

    # Without Mold Breaker, Levitate stays.
    def test_nothing_ignored(self):
        calc = make_calc("garchomp", "bronzong", "earthquake", defender_ability="levitate")
        calc.ignore_abilities()
        self.assertEqual(calc.defender_abilities, ["levitate"])

    # Mold Breaker ignores Levitate.
    def test_mold_breaker(self):
        calc = make_calc("excadrill", "bronzong", "earthquake", "mold-breaker", "levitate")
        calc.ignore_abilities()
        self.assertEqual(calc.defender_abilities, [""])

    # Ability Shield protects Levitate from Mold Breaker.
    def test_ability_shield(self):
        calc = make_calc("excadrill", "bronzong", "earthquake", "mold-breaker", "levitate", defender_item="ability-shield")
        calc.ignore_abilities()
        self.assertEqual(calc.defender_abilities, ["levitate"])

    # Mold Breaker can't ignore Shadow Shield.
    def test_unbreakable_ability(self):
        calc = make_calc("excadrill", "lunala", "earthquake", "mold-breaker", "shadow-shield")
        calc.ignore_abilities()
        self.assertEqual(calc.defender_abilities, ["shadow-shield"])

    # Sunsteel Strike ignores abilities like Mold Breaker does.
    def test_ability_ignoring_move(self):
        calc = make_calc("solgaleo", "bronzong", "sunsteel-strike", "full-metal-body", "levitate")
        calc.ignore_abilities()
        self.assertEqual(calc.defender_abilities, [""])

    # Neutralizing Gas turns off the other Pokémon's ability, on either side.
    def test_neutralizing_gas(self):
        attacking = make_calc("weezing", "bronzong", "sludge-bomb", "neutralizing-gas", "levitate")
        attacking.ignore_abilities()
        self.assertEqual((attacking.attacker_abilities, attacking.defender_abilities), (["neutralizing-gas"], [""]))
        defending = make_calc("garchomp", "weezing", "earthquake", "rough-skin", "neutralizing-gas")
        defending.ignore_abilities()
        self.assertEqual(defending.attacker_abilities, [""])


class MoveTypeTests(TestCase):
    fixtures = ["pokedata"]

    # Weather Ball's type matches the weather.
    def test_weather_ball(self):
        for weather, move_type in (("sun", "fire"), ("rain", "water"), ("sandstorm", "rock"), ("snow", "ice"), (None, "normal")):
            self.assertEqual(make_calc("castform", "snorlax", "weather-ball", weather=weather).final_move_type(), (move_type, False))

    # Utility Umbrella keeps Weather Ball Normal type in rain.
    def test_weather_ball_umbrella(self):
        self.assertEqual(make_calc("pelipper", "snorlax", "weather-ball", attacker_item="utility-umbrella", weather="rain").final_move_type(), ("normal", False))

    # Plates, Drives, Memories and Masks change Judgment, Techno Blast, Multi-Attack and Ivy Cudgel.
    def test_item_moves(self):
        self.assertEqual(make_calc("arceus", "snorlax", "judgment", attacker_item="zap-plate").final_move_type(), ("electric", False))
        self.assertEqual(make_calc("genesect", "snorlax", "techno-blast", attacker_item="douse-drive").final_move_type(), ("water", False))
        self.assertEqual(make_calc("silvally", "snorlax", "multi-attack", attacker_item="fire-memory").final_move_type(), ("fire", False))
        self.assertEqual(make_calc("ogerpon", "snorlax", "ivy-cudgel", attacker_item="hearthflame-mask").final_move_type(), ("fire", False))
        self.assertEqual(make_calc("ogerpon", "snorlax", "ivy-cudgel").final_move_type(), ("grass", False))

    # Terrain Pulse matches the terrain, but only if the user is on the ground.
    def test_terrain_pulse(self):
        grounded = DamageCalculator(Species.objects.get(name="garchomp"), Species.objects.get(name="snorlax"), "normal", 50, "terrain-pulse", None, None, terrain="electric-terrain")
        flying = DamageCalculator(Species.objects.get(name="pelipper"), Species.objects.get(name="snorlax"), "normal", 50, "terrain-pulse", None, None, terrain="electric-terrain")
        self.assertEqual(grounded.final_move_type(), ("electric", False))
        self.assertEqual(flying.final_move_type(), ("normal", False))

    # Revelation Dance matches the user's first type.
    def test_revelation_dance(self):
        self.assertEqual(make_calc("charizard", "snorlax", "revelation-dance").final_move_type(), ("fire", False))

    # Hidden Power's type comes from the IVs. All 31 IVs is Dark.
    def test_hidden_power(self):
        self.assertEqual(make_calc("snorlax", "machamp", "hidden-power").final_move_type(), ("dark", False))
        fire_ivs = {"attack": 30, "special-attack": 30, "speed": 30}
        self.assertEqual(make_calc("snorlax", "machamp", "hidden-power", attacker_build={"ivs": fire_ivs}).final_move_type(), ("fire", False))
        ice_ivs = {"attack": 30, "defense": 30}
        self.assertEqual(make_calc("snorlax", "machamp", "hidden-power", attacker_build={"ivs": ice_ivs}).final_move_type(), ("ice", False))

    # Struggle has no type.
    def test_struggle(self):
        self.assertEqual(make_calc("snorlax", "gengar", "struggle").final_move_type(), ("typeless", False))

    # Pixilate turns Normal moves Fairy type (and boosts them), but leaves other types alone.
    def test_pixilate(self):
        self.assertEqual(make_calc("sylveon", "snorlax", "hyper-voice", attacker_ability="pixilate").final_move_type(), ("fairy", True))
        self.assertEqual(make_calc("sylveon", "snorlax", "shadow-ball", attacker_ability="pixilate").final_move_type(), ("ghost", False))

    # Normalize turns every move Normal type and boosts it, even moves that were already Normal.
    def test_normalize(self):
        self.assertEqual(make_calc("delcatty", "snorlax", "shadow-ball", attacker_ability="normalize").final_move_type(), ("normal", True))
        self.assertEqual(make_calc("delcatty", "snorlax", "hyper-voice", attacker_ability="normalize").final_move_type(), ("normal", True))

    # Liquid Voice turns sound moves Water type, without a power boost.
    def test_liquid_voice(self):
        self.assertEqual(make_calc("primarina", "snorlax", "boomburst", attacker_ability="liquid-voice").final_move_type(), ("water", False))

    # A move with nothing changing it keeps its type.
    def test_no_change(self):
        self.assertEqual(make_calc("gengar", "snorlax", "shadow-ball").final_move_type(), ("ghost", False))

    # Aerilate, Galvanize and Refrigerate turn Normal moves Flying, Electric and Ice, and boost them.
    def test_other_ate_abilities(self):
        self.assertEqual(make_calc("salamence", "snorlax", "double-edge", attacker_ability="aerilate").final_move_type(), ("flying", True))
        self.assertEqual(make_calc("golem", "snorlax", "double-edge", attacker_ability="galvanize").final_move_type(), ("electric", True))
        self.assertEqual(make_calc("glalie", "snorlax", "double-edge", attacker_ability="refrigerate").final_move_type(), ("ice", True))
class TypeEffectivenessTests(TestCase):
    fixtures = ["pokedata"]

    # Fire is super effective on Grass.
    def test_super_effective(self):
        self.assertEqual(make_calc("charizard", "venusaur", "flamethrower").final_type_effectiveness(155), 2)

    # Normal moves can't hit Ghost types.
    def test_immune(self):
        self.assertEqual(make_calc("snorlax", "gengar", "tackle").final_type_effectiveness(120), 0)

    # Scrappy lets Normal moves hit Ghost types. Normal is neutral on Gengar's Poison type.
    def test_scrappy(self):
        self.assertEqual(make_calc("kangaskhan", "gengar", "tackle", attacker_ability="scrappy").final_type_effectiveness(120), 1)

    # Mind's Eye lets Fighting moves hit Ghost types. Fighting is resisted by Gengar's Poison type.
    def test_minds_eye(self):
        self.assertEqual(make_calc("machamp", "gengar", "close-combat", attacker_ability="minds-eye").final_type_effectiveness(120), 0.5)

    # Freeze-Dry is super effective on Water types.
    def test_freeze_dry(self):
        self.assertEqual(make_calc("lapras", "vaporeon", "freeze-dry").final_type_effectiveness(205), 2)
        self.assertEqual(make_calc("lapras", "gyarados", "freeze-dry").final_type_effectiveness(170), 4)

    # Flying Press counts as Fighting and Flying. Both are super effective on Breloom.
    def test_flying_press(self):
        self.assertEqual(make_calc("hawlucha", "breloom", "flying-press").final_type_effectiveness(135), 4)

    # Ring Target removes immunities, so Normal moves hit Gengar for neutral dmg.
    def test_ring_target(self):
        self.assertEqual(make_calc("snorlax", "gengar", "tackle", defender_item="ring-target").final_type_effectiveness(120), 1)

    # Iron Ball and Thousand Arrows let Ground moves hit Flying types for neutral dmg.
    def test_iron_ball_and_thousand_arrows(self):
        self.assertEqual(make_calc("garchomp", "charizard", "earthquake").final_type_effectiveness(153), 0)
        self.assertEqual(make_calc("garchomp", "charizard", "earthquake", defender_item="iron-ball").final_type_effectiveness(153), 1)
        self.assertEqual(make_calc("garchomp", "charizard", "thousand-arrows").final_type_effectiveness(153), 1)

    # Strong winds take away the Flying type's weaknesses, so Electric only hits Gyarados 2x instead of 4x.
    def test_strong_winds(self):
        self.assertEqual(make_calc("pikachu", "gyarados", "thunderbolt").final_type_effectiveness(170), 4)
        self.assertEqual(make_calc("pikachu", "gyarados", "thunderbolt", weather="strong-winds").final_type_effectiveness(170), 2)

    # Tera Shell makes every hit not very effective while Terapagos is at full HP.
    def test_tera_shell(self):
        calc = make_calc("machamp", "terapagos", "close-combat", defender_ability="tera-shell")
        full_hp = calc.defender_stats["hp"]
        self.assertEqual(calc.final_type_effectiveness(full_hp), 0.5)
        self.assertEqual(calc.final_type_effectiveness(full_hp - 1), 2)

    # Bug is resisted by both of Charizard's types, so it's 1/4.
    def test_double_resist(self):
        self.assertEqual(make_calc("butterfree", "charizard", "bug-buzz").final_type_effectiveness(153), 0.25)

class MoveBlockedTests(TestCase):
    fixtures = ["pokedata"]

    # A normal hit isn't blocked.
    def test_not_blocked(self):
        self.assertFalse(make_calc("machamp", "snorlax", "close-combat").move_blocked())

    # Levitate and Air Balloon block Ground moves, but Thousand Arrows still hits.
    def test_levitate_and_air_balloon(self):
        self.assertTrue(make_calc("garchomp", "bronzong", "earthquake", defender_ability="levitate").move_blocked())
        self.assertTrue(make_calc("garchomp", "heatran", "earthquake", defender_item="air-balloon").move_blocked())
        self.assertFalse(make_calc("garchomp", "bronzong", "thousand-arrows", defender_ability="levitate").move_blocked())

    # Abilities that absorb a type block moves of that type.
    def test_absorb_abilities(self):
        for move, ability in (("flamethrower", "flash-fire"), ("flamethrower", "well-baked-body"), ("surf", "water-absorb"), ("surf", "storm-drain"), ("surf", "dry-skin"),
                              ("thunderbolt", "volt-absorb"), ("thunderbolt", "lightning-rod"), ("thunderbolt", "motor-drive"), ("energy-ball", "sap-sipper"),
                              ("earthquake", "earth-eater"), ("hurricane", "wind-rider")):
            self.assertTrue(make_calc("mew", "snorlax", move, defender_ability=ability).move_blocked(), f"{ability} should block {move}")

    # Soundproof blocks sound moves and Bulletproof blocks ball and bomb moves.
    def test_soundproof_and_bulletproof(self):
        self.assertTrue(make_calc("mew", "snorlax", "boomburst", defender_ability="soundproof").move_blocked())
        self.assertTrue(make_calc("mew", "snorlax", "shadow-ball", defender_ability="bulletproof").move_blocked())

    # Wonder Guard only lets super-effective moves hit.
    def test_wonder_guard(self):
        self.assertFalse(make_calc("charizard", "shedinja", "flamethrower", defender_ability="wonder-guard").move_blocked())
        self.assertTrue(make_calc("scizor", "shedinja", "x-scissor", defender_ability="wonder-guard").move_blocked())

    # Queenly Majesty, Dazzling and Armor Tail block priority moves only.
    def test_priority_blockers(self):
        for ability in ("queenly-majesty", "dazzling", "armor-tail"):
            self.assertTrue(make_calc("snorlax", "machamp", "quick-attack", defender_ability=ability).move_blocked())
        self.assertFalse(make_calc("snorlax", "machamp", "tackle", defender_ability="queenly-majesty").move_blocked())

    # Psychic Terrain blocks priority moves on Pokémon that are on the ground.
    def test_psychic_terrain(self):
        grounded = DamageCalculator(Species.objects.get(name="snorlax"), Species.objects.get(name="machamp"), "normal", 40, "quick-attack", None, None, terrain="psychic-terrain")
        flying = DamageCalculator(Species.objects.get(name="snorlax"), Species.objects.get(name="pelipper"), "normal", 40, "quick-attack", None, None, terrain="psychic-terrain")
        self.assertTrue(grounded.move_blocked())
        self.assertFalse(flying.move_blocked())

    # Heavy rain stops Fire moves and extreme sun stops Water moves. Regular sun doesn't stop anything.
    def test_primal_weather(self):
        self.assertTrue(make_calc("charizard", "snorlax", "flamethrower", weather="heavy-rain").move_blocked())
        self.assertTrue(make_calc("blastoise", "snorlax", "surf", weather="extreme-sun").move_blocked())
        self.assertFalse(make_calc("blastoise", "snorlax", "surf", weather="sun").move_blocked())

    # Poltergeist needs the target to hold an item, Dream Eater needs it asleep and Steel Roller needs terrain.
    def test_moves_that_need_something(self):
        self.assertTrue(make_calc("gengar", "machamp", "poltergeist").move_blocked())
        self.assertFalse(make_calc("gengar", "machamp", "poltergeist", defender_item="leftovers").move_blocked())
        awake = DamageCalculator(Species.objects.get(name="gengar"), Species.objects.get(name="machamp"), "psychic", 100, "dream-eater", None, None)
        asleep = DamageCalculator(Species.objects.get(name="gengar"), Species.objects.get(name="machamp"), "psychic", 100, "dream-eater", None, "sleep")
        self.assertTrue(awake.move_blocked())
        self.assertFalse(asleep.move_blocked())
        self.assertTrue(make_calc("melmetal", "machamp", "steel-roller").move_blocked())

    # Synchronoise needs a shared type, Burn Up needs a Fire-type user, Double Shock needs an Electric-type user, and Sky Drop can't lift Flying types or anything 200 kg or heavier.
    def test_moves_that_need_types_or_weight(self):
        self.assertTrue(make_calc("alakazam", "machamp", "synchronoise").move_blocked())
        self.assertFalse(make_calc("alakazam", "espeon", "synchronoise").move_blocked())
        self.assertTrue(make_calc("snorlax", "machamp", "burn-up").move_blocked())
        self.assertFalse(make_calc("charizard", "machamp", "burn-up").move_blocked())
        self.assertTrue(make_calc("snorlax", "machamp", "double-shock").move_blocked())
        self.assertTrue(make_calc("charizard", "pelipper", "sky-drop").move_blocked())
        self.assertTrue(make_calc("charizard", "snorlax", "sky-drop").move_blocked())
        self.assertFalse(make_calc("charizard", "machamp", "sky-drop").move_blocked())

class FixedDamageTests(TestCase):
    fixtures = ["pokedata"]

    # Seismic Toss does dmg equal to the user's level.
    def test_seismic_toss(self):
        self.assertEqual(make_calc("machamp", "snorlax", "seismic-toss").fixed_damage(165, 235), 50)
        self.assertEqual(make_calc("machamp", "snorlax", "seismic-toss", attacker_build={"level": 37}).fixed_damage(165, 235), 37)

    # Night Shade works the same way.
    def test_night_shade(self):
        self.assertEqual(make_calc("gengar", "snorlax", "night-shade").fixed_damage(120, 235), 50)

    # Dragon Rage always does 40 and Sonic Boom always does 20.
    def test_dragon_rage_and_sonic_boom(self):
        self.assertEqual(make_calc("dragonite", "snorlax", "dragon-rage").fixed_damage(166, 235), 40)
        self.assertEqual(make_calc("magnezone", "snorlax", "sonic-boom").fixed_damage(130, 235), 20)

    # Super Fang does half the target's current HP, and always at least 1.
    def test_super_fang(self):
        self.assertEqual(make_calc("raticate", "snorlax", "super-fang").fixed_damage(115, 157), 78)
        self.assertEqual(make_calc("raticate", "snorlax", "super-fang").fixed_damage(115, 1), 1)

    # Ruination and Nature's Madness work the same way.
    def test_ruination_and_natures_madness(self):
        self.assertEqual(make_calc("chien-pao", "snorlax", "ruination").fixed_damage(155, 200), 100)
        self.assertEqual(make_calc("tapu-lele", "snorlax", "natures-madness").fixed_damage(130, 200), 100)

    # Final Gambit does dmg equal to the user's HP.
    def test_final_gambit(self):
        self.assertEqual(make_calc("machamp", "snorlax", "final-gambit").fixed_damage(120, 235), 120)

    # Endeavor brings the target down to the user's HP, and does nothing if the user has more HP.
    def test_endeavor(self):
        self.assertEqual(make_calc("machamp", "snorlax", "endeavor").fixed_damage(10, 150), 140)
        self.assertEqual(make_calc("machamp", "snorlax", "endeavor").fixed_damage(150, 10), 0)

    # One-hit KO moves do all the target's HP, unless it has Sturdy, is a higher level, or is an Ice type hit by Sheer Cold.
    def test_ohko_moves(self):
        self.assertEqual(make_calc("garchomp", "snorlax", "fissure").fixed_damage(183, 200), 200)
        self.assertEqual(make_calc("garchomp", "snorlax", "fissure", defender_ability="sturdy").fixed_damage(183, 200), 0)
        self.assertEqual(make_calc("garchomp", "snorlax", "fissure", attacker_build={"level": 40}).fixed_damage(150, 200), 0)
        self.assertEqual(make_calc("lapras", "lapras", "sheer-cold").fixed_damage(205, 205), 0)
        self.assertEqual(make_calc("lapras", "snorlax", "sheer-cold").fixed_damage(205, 235), 235)

    # Regular moves don't do set dmg.
    def test_regular_move(self):
        self.assertIsNone(make_calc("snorlax", "machamp", "tackle").fixed_damage(235, 165))


class KnockOffTests(TestCase):
    fixtures = ["pokedata"]

    # Knock Off gets its boost when the target is holding an item.
    def test_has_item(self):
        self.assertTrue(make_calc("weavile", "snorlax", "knock-off", defender_item="leftovers").knock_off_boost())

    # No item means no boost.
    def test_no_item(self):
        self.assertFalse(make_calc("weavile", "snorlax", "knock-off").knock_off_boost())

    # Groudon's Red Orb can't be knocked off, but anyone else's can.
    def test_primal_orbs(self):
        self.assertFalse(make_calc("weavile", "groudon", "knock-off", defender_item="red-orb").knock_off_boost())
        self.assertTrue(make_calc("weavile", "snorlax", "knock-off", defender_item="red-orb").knock_off_boost())

    # Arceus's Plates and Silvally's Memories can't be knocked off.
    def test_arceus_and_silvally(self):
        self.assertFalse(make_calc("weavile", "arceus", "knock-off", defender_item="flame-plate").knock_off_boost())
        self.assertTrue(make_calc("weavile", "snorlax", "knock-off", defender_item="flame-plate").knock_off_boost())
        self.assertFalse(make_calc("weavile", "silvally", "knock-off", defender_item="fire-memory").knock_off_boost())

    # Genesect's Drives can't be knocked off.
    def test_genesect_drives(self):
        self.assertFalse(make_calc("weavile", "genesect", "knock-off", defender_item="douse-drive").knock_off_boost())
        self.assertTrue(make_calc("weavile", "snorlax", "knock-off", defender_item="douse-drive").knock_off_boost())

    # The Origin Forme items and Ogerpon's masks can't be knocked off.
    def test_origin_items_and_masks(self):
        self.assertFalse(make_calc("weavile", "dialga", "knock-off", defender_item="adamant-crystal").knock_off_boost())
        self.assertFalse(make_calc("weavile", "ogerpon", "knock-off", defender_item="wellspring-mask").knock_off_boost())

    # Booster Energy can't be knocked off a Pokémon with Protosynthesis or Quark Drive.
    def test_booster_energy(self):
        self.assertFalse(make_calc("weavile", "great-tusk", "knock-off", defender_ability="protosynthesis", defender_item="booster-energy").knock_off_boost())
        self.assertTrue(make_calc("weavile", "snorlax", "knock-off", defender_item="booster-energy").knock_off_boost())

    # Memories and masks can be knocked off a Pokémon that isn't Silvally or Ogerpon.
    def test_other_pokemon_items(self):
        self.assertTrue(make_calc("weavile", "snorlax", "knock-off", defender_item="fire-memory").knock_off_boost())
        self.assertTrue(make_calc("weavile", "snorlax", "knock-off", defender_item="wellspring-mask").knock_off_boost())


class VariablePowerTests(TestCase):
    fixtures = ["pokedata"]

    # Calls variable_move_power with full HP, no stat stages and equal Speed unless a test says otherwise.
    def power(self, calc, attacker_hp=None, defender_hp=None, attacker_boosts=None, defender_boosts=None, attacker_speed=100, defender_speed=100):
        attacker_hp = calc.attacker_stats["hp"] if attacker_hp is None else attacker_hp
        defender_hp = calc.defender_stats["hp"] if defender_hp is None else defender_hp
        return calc.variable_move_power(attacker_hp, defender_hp, attacker_boosts or {}, defender_boosts or {}, attacker_speed, defender_speed)

    # Regular moves keep their power.
    def test_regular_move(self):
        self.assertEqual(self.power(make_calc("snorlax", "machamp", "tackle")), 40)

    # Moves that do set dmg have no power.
    def test_set_damage_move(self):
        self.assertIsNone(self.power(make_calc("machamp", "snorlax", "seismic-toss")))

    # Low Kick gets stronger the heavier the target is. Float Stone and Heavy Metal change the target's weight.
    def test_low_kick(self):
        self.assertEqual(self.power(make_calc("machamp", "snorlax", "low-kick")), 120)
        self.assertEqual(self.power(make_calc("machamp", "machamp", "low-kick")), 100)
        self.assertEqual(self.power(make_calc("machamp", "pikachu", "low-kick")), 20)
        self.assertEqual(self.power(make_calc("machamp", "machamp", "low-kick", defender_item="float-stone")), 80)
        self.assertEqual(self.power(make_calc("machamp", "machamp", "low-kick", defender_ability="heavy-metal")), 120)

    # Heavy Slam gets stronger the heavier the user is compared to the target.
    def test_heavy_slam(self):
        self.assertEqual(self.power(make_calc("snorlax", "pikachu", "heavy-slam")), 120)
        self.assertEqual(self.power(make_calc("snorlax", "machamp", "heavy-slam")), 80)
        self.assertEqual(self.power(make_calc("snorlax", "snorlax", "heavy-slam")), 40)

    # Gyro Ball is stronger when the user is slower. Electro Ball is stronger when the user is faster.
    def test_speed_moves(self):
        self.assertEqual(self.power(make_calc("ferrothorn", "machamp", "gyro-ball"), attacker_speed=30, defender_speed=200), 150)
        self.assertEqual(self.power(make_calc("ferrothorn", "machamp", "gyro-ball")), 26)
        self.assertEqual(self.power(make_calc("pikachu", "machamp", "electro-ball"), attacker_speed=200, defender_speed=50), 150)
        self.assertEqual(self.power(make_calc("pikachu", "machamp", "electro-ball")), 60)
        self.assertEqual(self.power(make_calc("pikachu", "machamp", "electro-ball"), attacker_speed=50, defender_speed=100), 40)

    # Eruption gets weaker as the user loses HP.
    def test_eruption(self):
        calc = make_calc("typhlosion", "snorlax", "eruption")
        max_hp = calc.attacker_stats["hp"]
        self.assertEqual(self.power(calc), 150)
        self.assertEqual(self.power(calc, attacker_hp=max_hp // 2), 74)
        self.assertEqual(self.power(calc, attacker_hp=1), 1)

    # Flail gets stronger as the user loses HP.
    def test_flail(self):
        calc = make_calc("magikarp", "snorlax", "flail")
        self.assertEqual(self.power(calc, attacker_hp=1), 200)
        self.assertEqual(self.power(calc), 20)

    # Crush Grip and Hard Press get weaker as the target loses HP.
    def test_crush_grip_and_hard_press(self):
        crush_grip = make_calc("regigigas", "pikachu", "crush-grip")
        self.assertEqual(self.power(crush_grip), 120)
        self.assertEqual(self.power(crush_grip, defender_hp=crush_grip.defender_stats["hp"] // 2), 60)
        self.assertEqual(self.power(make_calc("regigigas", "snorlax", "hard-press")), 100)

    # Stored Power counts the user's stat boosts and Punishment counts the target's. Drops don't count.
    def test_stat_stage_moves(self):
        self.assertEqual(self.power(make_calc("espeon", "snorlax", "stored-power"), attacker_boosts={"attack": 2, "speed": 1}), 80)
        self.assertEqual(self.power(make_calc("espeon", "snorlax", "stored-power"), attacker_boosts={"attack": -2}), 20)
        self.assertEqual(self.power(make_calc("weavile", "snorlax", "punishment"), defender_boosts={"defense": 3}), 120)

    # Return and Frustration have 102 power at max friendship.
    def test_friendship_moves(self):
        self.assertEqual(self.power(make_calc("snorlax", "machamp", "return")), 102)
        self.assertEqual(self.power(make_calc("snorlax", "machamp", "frustration")), 102)

    # Weather Ball doubles in weather (not with Utility Umbrella in rain). Terrain Pulse, Rising Voltage and Psyblade get stronger in terrain.
    def test_weather_and_terrain(self):
        self.assertEqual(self.power(make_calc("castform", "snorlax", "weather-ball", weather="rain")), 100)
        self.assertEqual(self.power(make_calc("pelipper", "snorlax", "weather-ball", attacker_item="utility-umbrella", weather="rain")), 50)
        self.assertEqual(self.power(make_calc("castform", "snorlax", "weather-ball", weather="strong-winds")), 50)
        electric = DamageCalculator(Species.objects.get(name="pikachu"), Species.objects.get(name="snorlax"), "electric", 70, "rising-voltage", None, None, terrain="electric-terrain")
        self.assertEqual(self.power(electric), 140)
        terrain_pulse = DamageCalculator(Species.objects.get(name="pikachu"), Species.objects.get(name="snorlax"), "normal", 50, "terrain-pulse", None, None, terrain="electric-terrain")
        self.assertEqual(self.power(terrain_pulse), 100)
        psyblade = DamageCalculator(Species.objects.get(name="iron-leaves"), Species.objects.get(name="snorlax"), "psychic", 80, "psyblade", None, None, terrain="electric-terrain")
        self.assertEqual(self.power(psyblade), 120)

    # Hex doubles on a target with a status, Acrobatics doubles with no item, Payback doubles moving last and Bolt Beak doubles moving first.
    def test_doubled_power(self):
        burned = DamageCalculator(Species.objects.get(name="gengar"), Species.objects.get(name="machamp"), "ghost", 65, "hex", None, "burn")
        self.assertEqual(self.power(burned), 130)
        self.assertEqual(self.power(make_calc("gengar", "machamp", "hex")), 65)
        self.assertEqual(self.power(make_calc("hawlucha", "machamp", "acrobatics")), 110)
        self.assertEqual(self.power(make_calc("hawlucha", "machamp", "acrobatics", attacker_item="leftovers")), 55)
        self.assertEqual(self.power(make_calc("umbreon", "machamp", "payback"), attacker_speed=50, defender_speed=100), 100)
        self.assertEqual(self.power(make_calc("umbreon", "machamp", "payback"), attacker_speed=150, defender_speed=100), 50)
        self.assertEqual(self.power(make_calc("dracozolt", "machamp", "bolt-beak"), attacker_speed=150, defender_speed=100), 170)

    # Heat Crash works like Heavy Slam and Water Spout works like Eruption.
    def test_heat_crash_and_water_spout(self):
        self.assertEqual(self.power(make_calc("snorlax", "pikachu", "heat-crash")), 120)
        calc = make_calc("kyogre", "snorlax", "water-spout")
        self.assertEqual(self.power(calc), 150)
        self.assertEqual(self.power(calc, attacker_hp=70), 60)

    # Power Trip works like Stored Power, and Punishment caps at 200.
    def test_power_trip_and_punishment_cap(self):
        self.assertEqual(self.power(make_calc("grimmsnarl", "snorlax", "power-trip"), attacker_boosts={"attack": 1, "defense": 1, "speed": 1}), 80)
        self.assertEqual(self.power(make_calc("weavile", "snorlax", "punishment"), defender_boosts={"attack": 6, "defense": 6}), 200)

    # Wake-Up Slap doubles on a sleeping target, Smelling Salts on a paralyzed one and Barb Barrage on a poisoned one.
    def test_status_doubled_moves(self):
        asleep = DamageCalculator(Species.objects.get(name="machamp"), Species.objects.get(name="snorlax"), "fighting", 70, "wake-up-slap", None, "sleep")
        self.assertEqual(self.power(asleep), 140)
        paralyzed = DamageCalculator(Species.objects.get(name="snorlax"), Species.objects.get(name="machamp"), "normal", 70, "smelling-salts", None, "paralysis")
        self.assertEqual(self.power(paralyzed), 140)
        poisoned = DamageCalculator(Species.objects.get(name="qwilfish"), Species.objects.get(name="machamp"), "poison", 60, "barb-barrage", None, "poison")
        self.assertEqual(self.power(poisoned), 120)

class HitCountTests(TestCase):
    fixtures = ["pokedata"]

    # Most moves only hit once.
    def test_single_hit(self):
        self.assertEqual(make_calc("snorlax", "machamp", "tackle").damage_calc_hits(), 1)

    # Moves like Double Kick always hit the same number of times.
    def test_fixed_hits(self):
        self.assertEqual(make_calc("machamp", "snorlax", "double-kick").damage_calc_hits(), 2)

    # 2-5 hit moves count as 3 hits.
    def test_two_to_five_hits(self):
        self.assertEqual(make_calc("cinccino", "snorlax", "bullet-seed").damage_calc_hits(), 3)

    # Skill Link makes 2-5 hit moves always hit 5 times.
    def test_skill_link(self):
        self.assertEqual(make_calc("cinccino", "snorlax", "bullet-seed", attacker_ability="skill-link").damage_calc_hits(), 5)

    # Skill Link doesn't change moves that already hit a set number of times.
    def test_skill_link_fixed_hits(self):
        self.assertEqual(make_calc("machamp", "snorlax", "double-kick", attacker_ability="skill-link").damage_calc_hits(), 2)

    # Loaded Dice makes 2-5 hit moves count as 4 hits, but does nothing for single hit moves.
    def test_loaded_dice(self):
        self.assertEqual(make_calc("cinccino", "snorlax", "bullet-seed", attacker_item="loaded-dice").damage_calc_hits(), 4)
        self.assertEqual(make_calc("snorlax", "machamp", "tackle", attacker_item="loaded-dice").damage_calc_hits(), 1)

    # Klutz stops Loaded Dice from working.
    def test_klutz_loaded_dice(self):
        self.assertEqual(make_calc("lopunny", "snorlax", "bullet-seed", attacker_ability="klutz", attacker_item="loaded-dice").damage_calc_hits(), 3)

    # Newer moves PokeAPI is missing hit counts for.
    def test_missing_hit_counts(self):
        self.assertEqual(make_calc("maushold-family-of-four", "snorlax", "population-bomb").damage_calc_hits(), 10)
        self.assertEqual(make_calc("dondozo", "snorlax", "triple-dive").damage_calc_hits(), 3)
        self.assertEqual(make_calc("espeon", "snorlax", "twin-beam").damage_calc_hits(), 2)

    # Dragon Darts always hits twice and Surging Strikes always hits 3 times.
    def test_more_fixed_hits(self):
        self.assertEqual(make_calc("dragapult", "snorlax", "dragon-darts").damage_calc_hits(), 2)
        self.assertEqual(make_calc("urshifu-single-strike", "snorlax", "surging-strikes").damage_calc_hits(), 3)


class BasePowerTests(TestCase):
    fixtures = ["pokedata"]

    # Calls final_base_power as a physical, neutral, non-contact move at full HP unless a test says otherwise.
    def power(self, calc, physical=True, effectiveness=1, ate_changed=False, moves_last=False, makes_contact=False, attacker_boosts=None, defender_hp=None):
        defender_hp = calc.defender_stats["hp"] if defender_hp is None else defender_hp
        return calc.final_base_power(physical, effectiveness, ate_changed, moves_last, makes_contact, attacker_boosts or {}, defender_hp)

    # Regular moves keep their power.
    def test_regular_move(self):
        self.assertEqual(self.power(make_calc("snorlax", "machamp", "tackle")), 40)

    # Facade doubles when the user has a status and Brine doubles when the target is at half HP or less.
    def test_facade_and_brine(self):
        burned = DamageCalculator(Species.objects.get(name="snorlax"), Species.objects.get(name="machamp"), "normal", 70, "facade", "burn", None)
        self.assertEqual(self.power(burned), 140)
        self.assertEqual(self.power(make_calc("snorlax", "machamp", "facade")), 70)
        self.assertEqual(self.power(make_calc("pelipper", "snorlax", "brine"), physical=False, defender_hp=117), 130)
        self.assertEqual(self.power(make_calc("pelipper", "snorlax", "brine"), physical=False), 65)

    # Knock Off's 1.5x boost makes 97.5 power, and .5 rounds down.
    def test_knock_off(self):
        self.assertEqual(self.power(make_calc("weavile", "snorlax", "knock-off", defender_item="leftovers")), 97)
        self.assertEqual(self.power(make_calc("weavile", "snorlax", "knock-off")), 65)

    # Solar Beam is halved in rain, but not in sun or with Utility Umbrella.
    def test_solar_beam(self):
        self.assertEqual(self.power(make_calc("venusaur", "snorlax", "solar-beam", weather="rain"), physical=False), 60)
        self.assertEqual(self.power(make_calc("venusaur", "snorlax", "solar-beam", weather="sun"), physical=False), 120)
        self.assertEqual(self.power(make_calc("venusaur", "snorlax", "solar-beam", attacker_item="utility-umbrella", weather="rain"), physical=False), 120)

    # Collision Course gets 1.33x power when it's super effective.
    def test_collision_course(self):
        self.assertEqual(self.power(make_calc("koraidon", "snorlax", "collision-course"), effectiveness=2), 133)
        self.assertEqual(self.power(make_calc("koraidon", "snorlax", "collision-course")), 100)

    # Electric Terrain boosts Electric moves by 1.3x and Grassy Terrain halves Earthquake.
    def test_terrain(self):
        electric = DamageCalculator(Species.objects.get(name="pikachu"), Species.objects.get(name="machamp"), "electric", 90, "thunderbolt", None, None, terrain="electric-terrain")
        self.assertEqual(self.power(electric, physical=False), 117)
        grassy = DamageCalculator(Species.objects.get(name="garchomp"), Species.objects.get(name="snorlax"), "ground", 100, "earthquake", None, None, terrain="grassy-terrain")
        self.assertEqual(self.power(grassy), 50)

    # Technician boosts moves with 60 power or less by 1.5x.
    def test_technician(self):
        self.assertEqual(self.power(make_calc("scizor", "snorlax", "bullet-punch", attacker_ability="technician")), 60)
        self.assertEqual(self.power(make_calc("scizor", "snorlax", "iron-head", attacker_ability="technician")), 80)

    # Iron Fist boosts punches by 1.2x, Strong Jaw boosts bites by 1.5x and Sheer Force boosts moves with added effects by 1.3x.
    def test_boosting_abilities(self):
        self.assertEqual(self.power(make_calc("hitmonchan", "snorlax", "mach-punch", attacker_ability="iron-fist")), 48)
        self.assertEqual(self.power(make_calc("feraligatr", "snorlax", "crunch", attacker_ability="strong-jaw")), 120)
        self.assertEqual(self.power(make_calc("nidoking", "machamp", "flamethrower", attacker_ability="sheer-force"), physical=False), 117)

    # Tough Claws boosts contact moves by 1.3x and -ate abilities boost the moves they change by 1.2x.
    def test_tough_claws_and_ate(self):
        self.assertEqual(self.power(make_calc("barbaracle", "snorlax", "tackle", attacker_ability="tough-claws"), makes_contact=True), 52)
        self.assertEqual(self.power(make_calc("barbaracle", "snorlax", "tackle", attacker_ability="tough-claws")), 40)
        self.assertEqual(self.power(make_calc("sylveon", "snorlax", "hyper-voice"), physical=False, ate_changed=True), 108)

    # Type boosting items give 1.2x, Gems give 1.3x, Muscle Band boosts physical moves and Wise Glasses boosts special moves.
    def test_items(self):
        self.assertEqual(self.power(make_calc("charizard", "machamp", "flamethrower", attacker_item="charcoal"), physical=False), 108)
        self.assertEqual(self.power(make_calc("charizard", "machamp", "flamethrower", attacker_item="fire-gem"), physical=False), 117)
        self.assertEqual(self.power(make_calc("snorlax", "machamp", "tackle", attacker_item="muscle-band")), 44)
        self.assertEqual(self.power(make_calc("snorlax", "machamp", "tackle", attacker_item="wise-glasses")), 40)
        self.assertEqual(self.power(make_calc("charizard", "machamp", "flamethrower", attacker_item="wise-glasses"), physical=False), 99)

    # Abilities and items stack. Technician and Metal Coat on Bullet Punch is 40 x 1.5 x 1.2.
    def test_modifiers_stack(self):
        self.assertEqual(self.power(make_calc("scizor", "snorlax", "bullet-punch", attacker_ability="technician", attacker_item="metal-coat")), 72)

    # Venoshock doubles on a poisoned target and Expanding Force gets 1.5x in Psychic Terrain.
    def test_venoshock_and_expanding_force(self):
        poisoned = DamageCalculator(Species.objects.get(name="toxapex"), Species.objects.get(name="machamp"), "poison", 65, "venoshock", None, "poison")
        self.assertEqual(self.power(poisoned, physical=False), 130)
        expanding_force = DamageCalculator(Species.objects.get(name="alakazam"), Species.objects.get(name="machamp"), "psychic", 80, "expanding-force", None, None, terrain="psychic-terrain")
        self.assertEqual(self.power(expanding_force, physical=False), 156)

    # Mega Launcher boosts pulse moves by 1.5x and Reckless boosts recoil moves by 1.2x.
    def test_mega_launcher_and_reckless(self):
        self.assertEqual(self.power(make_calc("blastoise", "snorlax", "aura-sphere", attacker_ability="mega-launcher"), physical=False), 120)
        self.assertEqual(self.power(make_calc("staraptor", "snorlax", "double-edge", attacker_ability="reckless")), 144)


class AttackStatTests(TestCase):
    fixtures = ["pokedata"]

    # Calls final_attack as a physical, non-crit move at full HP with no stat stages unless a test says otherwise.
    def attack(self, calc, physical=True, critical_hit=False, attacker_boosts=None, defender_boosts=None, attacker_hp=None):
        attacker_hp = calc.attacker_stats["hp"] if attacker_hp is None else attacker_hp
        return calc.final_attack(physical, critical_hit, attacker_boosts or {}, defender_boosts or {}, attacker_hp)

    # Physical moves use Attack and special moves use Sp. Atk.
    def test_attack_and_special_attack(self):
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "earthquake")), 150)
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "draco-meteor"), physical=False), 100)

    # Stat stages raise or lower the stat. +2 doubles it and -1 makes it 2/3.
    def test_stat_stages(self):
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "earthquake"), attacker_boosts={"attack": 2}), 300)
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "earthquake"), attacker_boosts={"attack": -1}), 100)

    # Crits ignore the attacker's stat drops but keep its boosts.
    def test_critical_hit(self):
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "earthquake"), critical_hit=True, attacker_boosts={"attack": -1}), 150)
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "earthquake"), critical_hit=True, attacker_boosts={"attack": 1}), 225)

    # Unaware ignores the attacker's stat boosts.
    def test_unaware(self):
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "earthquake", defender_ability="unaware"), attacker_boosts={"attack": 2}), 150)

    # Foul Play uses the target's Attack and the target's stat stages.
    def test_foul_play(self):
        self.assertEqual(self.attack(make_calc("umbreon", "garchomp", "foul-play")), 150)
        self.assertEqual(self.attack(make_calc("umbreon", "garchomp", "foul-play"), defender_boosts={"attack": 1}), 225)
        self.assertEqual(self.attack(make_calc("umbreon", "garchomp", "foul-play"), attacker_boosts={"attack": 2}), 150)

    # Body Press uses the user's Defense and Defense stages.
    def test_body_press(self):
        self.assertEqual(self.attack(make_calc("snorlax", "machamp", "body-press")), 85)
        self.assertEqual(self.attack(make_calc("snorlax", "machamp", "body-press"), attacker_boosts={"defense": 1}), 127)

    # Huge Power doubles Attack. Hustle boosts Attack by 1.5x but not Sp. Atk.
    def test_huge_power_and_hustle(self):
        self.assertEqual(self.attack(make_calc("azumarill", "machamp", "aqua-tail", attacker_ability="huge-power")), 140)
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "earthquake", attacker_ability="hustle")), 225)
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "draco-meteor", attacker_ability="hustle"), physical=False), 100)

    # Choice Band and Choice Specs give 1.5x. Light Ball doubles Pikachu's Attack and Sp. Atk.
    def test_choice_items_and_light_ball(self):
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "earthquake", attacker_item="choice-band")), 225)
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "draco-meteor", attacker_item="choice-specs"), physical=False), 150)
        self.assertEqual(self.attack(make_calc("pikachu", "machamp", "volt-tackle", attacker_item="light-ball")), 150)
        self.assertEqual(self.attack(make_calc("pikachu", "machamp", "thunderbolt", attacker_item="light-ball"), physical=False), 140)

    # Blaze boosts Fire moves by 1.5x at 1/3 HP or less. 193.5 rounds down to 193.
    def test_blaze(self):
        calc = make_calc("charizard", "machamp", "flamethrower", attacker_ability="blaze")
        third = calc.attacker_stats["hp"] // 3
        self.assertEqual(self.attack(calc, physical=False, attacker_hp=third), 193)
        self.assertEqual(self.attack(calc, physical=False, attacker_hp=third + 1), 129)

    # Thick Fat halves Fire and Ice moves and Tablets of Ruin lowers the attacker's Attack to 0.75x.
    def test_defender_abilities(self):
        self.assertEqual(self.attack(make_calc("charizard", "snorlax", "flamethrower", defender_ability="thick-fat"), physical=False), 64)
        self.assertEqual(self.attack(make_calc("garchomp", "machamp", "earthquake", defender_ability="tablets-of-ruin")), 112)

class DefenseStatTests(TestCase):
    fixtures = ["pokedata"]

    # Calls final_defense as a physical, non-crit hit with no stat stages unless a test says otherwise.
    def defense(self, calc, physical_hit=True, critical_hit=False, attacker_boosts=None, defender_boosts=None):
        return calc.final_defense(physical_hit, critical_hit, attacker_boosts or {}, defender_boosts or {})

    # Physical hits use Defense and special hits use Sp. Def.
    def test_defense_and_special_defense(self):
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle")), 85)
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle"), physical_hit=False), 130)

    # Stat stages raise or lower the stat. +1 is 1.5x and -2 is 1/2.
    def test_stat_stages(self):
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle"), defender_boosts={"defense": 1}), 127)
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle"), defender_boosts={"defense": -2}), 42)

    # Crits ignore the defender's stat boosts but keep its drops.
    def test_critical_hit(self):
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle"), critical_hit=True, defender_boosts={"defense": 2}), 85)
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle"), critical_hit=True, defender_boosts={"defense": -1}), 56)

    # Unaware and Sacred Sword ignore the defender's stat changes.
    def test_unaware_and_sacred_sword(self):
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle", attacker_ability="unaware"), defender_boosts={"defense": 2}), 85)
        self.assertEqual(self.defense(make_calc("gallade", "snorlax", "sacred-sword"), defender_boosts={"defense": 2}), 85)
        self.assertEqual(self.defense(make_calc("gallade", "snorlax", "sacred-sword"), defender_boosts={"defense": -2}), 85)

    # Sandstorm boosts Rock types' Sp. Def by 1.5x, but not their Defense or anyone else's Sp. Def.
    def test_sandstorm(self):
        self.assertEqual(self.defense(make_calc("machamp", "tyranitar", "tackle", weather="sandstorm"), physical_hit=False), 180)
        self.assertEqual(self.defense(make_calc("machamp", "tyranitar", "tackle", weather="sandstorm")), 130)
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle", weather="sandstorm"), physical_hit=False), 130)

    # Snow boosts Ice types' Defense by 1.5x. 142.5 rounds down to 142.
    def test_snow(self):
        self.assertEqual(self.defense(make_calc("machamp", "abomasnow", "tackle", weather="snow")), 142)
        self.assertEqual(self.defense(make_calc("machamp", "abomasnow", "tackle")), 95)

    # Eviolite boosts both defenses by 1.5x, but only on Pokémon that can still evolve.
    def test_eviolite(self):
        self.assertEqual(self.defense(make_calc("machamp", "chansey", "tackle", defender_item="eviolite")), 37)
        self.assertEqual(self.defense(make_calc("machamp", "chansey", "tackle", defender_item="eviolite"), physical_hit=False), 187)
        self.assertEqual(self.defense(make_calc("machamp", "blissey", "tackle", defender_item="eviolite")), 30)

    # Assault Vest boosts Sp. Def by 1.5x but not Defense.
    def test_assault_vest(self):
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle", defender_item="assault-vest"), physical_hit=False), 195)
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle", defender_item="assault-vest")), 85)

    # Fur Coat doubles Defense and Marvel Scale boosts it by 1.5x when the defender has a status.
    def test_fur_coat_and_marvel_scale(self):
        self.assertEqual(self.defense(make_calc("machamp", "snorlax", "tackle", defender_ability="fur-coat")), 170)
        burned = DamageCalculator(Species.objects.get(name="machamp"), Species.objects.get(name="milotic"), "normal", 40, "tackle", None, "burn", defender_ability="marvel-scale")
        self.assertEqual(self.defense(burned), 148)
        self.assertEqual(self.defense(make_calc("machamp", "milotic", "tackle", defender_ability="marvel-scale")), 99)

    # Sword of Ruin lowers the defender's Defense and Beads of Ruin lowers its Sp. Def to 0.75x. 97.5 rounds down to 97.
    def test_ruin_abilities(self):
        self.assertEqual(self.defense(make_calc("chien-pao", "snorlax", "tackle", attacker_ability="sword-of-ruin")), 64)
        self.assertEqual(self.defense(make_calc("chi-yu", "snorlax", "tackle", attacker_ability="beads-of-ruin"), physical_hit=False), 97)

    # Metal Powder doubles Ditto's Defense.
    def test_metal_powder(self):
        calc = make_calc("machamp", "ditto", "close-combat", defender_item="metal-powder")
        self.assertEqual(self.defense(calc), calc.defender_stats["defense"] * 2)


class FinalModifierTests(TestCase):
    fixtures = ["pokedata"]

    # Calls final_modifiers as a physical, non-crit, neutral, non-contact first hit at full HP with no screens unless a test says otherwise.
    def modifier(self, calc, physical=True, critical_hit=False, effectiveness=1, defender_hp=None, makes_contact=False, reflect=False, light_screen=False, aurora_veil=False, first_hit=True):
        defender_hp = calc.defender_stats["hp"] if defender_hp is None else defender_hp
        return calc.final_modifiers(physical, critical_hit, effectiveness, defender_hp, makes_contact, reflect, light_screen, aurora_veil, first_hit)

    # No modifiers means 4096 (1x).
    def test_no_modifiers(self):
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle")), 4096)

    # Reflect halves physical dmg and Light Screen halves special dmg. Aurora Veil halves both.
    def test_screens(self):
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle"), reflect=True), 2048)
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle"), physical=False, reflect=True), 4096)
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle"), physical=False, light_screen=True), 2048)
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle"), aurora_veil=True), 2048)
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle"), physical=False, aurora_veil=True), 2048)

    # Crits, Infiltrator and Brick Break go through screens.
    def test_ignoring_screens(self):
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle"), critical_hit=True, reflect=True), 4096)
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle", attacker_ability="infiltrator"), reflect=True), 4096)
        self.assertEqual(self.modifier(make_calc("machamp", "snorlax", "brick-break"), reflect=True), 4096)

    # Multiscale halves dmg at full HP, but only on the first hit.
    def test_multiscale(self):
        calc = make_calc("garchomp", "dragonite", "dragon-claw", defender_ability="multiscale")
        self.assertEqual(self.modifier(calc), 2048)
        self.assertEqual(self.modifier(calc, defender_hp=calc.defender_stats["hp"] - 1), 4096)
        self.assertEqual(self.modifier(calc, first_hit=False), 4096)

    # Filter takes 0.75x dmg from super effective moves.
    def test_filter(self):
        self.assertEqual(self.modifier(make_calc("machamp", "mr-mime", "tackle", defender_ability="filter"), effectiveness=2), 3072)
        self.assertEqual(self.modifier(make_calc("machamp", "mr-mime", "tackle", defender_ability="filter")), 4096)

    # Fluffy halves contact dmg but doubles Fire dmg. A Fire contact move cancels out.
    def test_fluffy(self):
        self.assertEqual(self.modifier(make_calc("machamp", "dubwool", "tackle", defender_ability="fluffy"), makes_contact=True), 2048)
        self.assertEqual(self.modifier(make_calc("charizard", "dubwool", "flamethrower", defender_ability="fluffy"), physical=False), 8192)
        self.assertEqual(self.modifier(make_calc("charizard", "dubwool", "fire-punch", defender_ability="fluffy"), makes_contact=True), 4096)

    # Ice Scales halves special dmg but not physical dmg.
    def test_ice_scales(self):
        self.assertEqual(self.modifier(make_calc("charizard", "frosmoth", "flamethrower", defender_ability="ice-scales"), physical=False), 2048)
        self.assertEqual(self.modifier(make_calc("machamp", "frosmoth", "tackle", defender_ability="ice-scales")), 4096)

    # Tinted Lens doubles not very effective dmg, Neuroforce gives 1.25x on super effective hits and Sniper gives 1.5x on crits.
    def test_attacker_abilities(self):
        self.assertEqual(self.modifier(make_calc("butterfree", "machamp", "bug-buzz", attacker_ability="tinted-lens"), physical=False, effectiveness=0.5), 8192)
        self.assertEqual(self.modifier(make_calc("necrozma", "machamp", "psychic", attacker_ability="neuroforce"), physical=False, effectiveness=2), 5120)
        self.assertEqual(self.modifier(make_calc("kingdra", "machamp", "surf", attacker_ability="sniper"), physical=False, critical_hit=True), 6144)

    # Expert Belt gives 1.2x on super effective hits and Life Orb gives 1.3x on everything.
    def test_expert_belt_and_life_orb(self):
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle", attacker_item="expert-belt"), effectiveness=2), 4915)
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle", attacker_item="expert-belt")), 4096)
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle", attacker_item="life-orb")), 5324)

    # Resist Berries halve super effective dmg of their type on the first hit. Ripen makes it 1/4 and Chilan Berry works on neutral Normal moves.
    def test_resist_berries(self):
        self.assertEqual(self.modifier(make_calc("charizard", "scizor", "flamethrower", defender_item="occa-berry"), physical=False, effectiveness=4), 2048)
        self.assertEqual(self.modifier(make_calc("charizard", "snorlax", "flamethrower", defender_item="occa-berry"), physical=False), 4096)
        self.assertEqual(self.modifier(make_calc("charizard", "scizor", "flamethrower", defender_ability="ripen", defender_item="occa-berry"), physical=False, effectiveness=4), 1024)
        self.assertEqual(self.modifier(make_calc("charizard", "scizor", "flamethrower", defender_item="occa-berry"), physical=False, effectiveness=4, first_hit=False), 4096)
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle", defender_item="chilan-berry")), 2048)

    # Unnerve stops the defender from eating its Berry.
    def test_unnerve(self):
        self.assertEqual(self.modifier(make_calc("charizard", "scizor", "flamethrower", attacker_ability="unnerve", defender_item="occa-berry"), physical=False, effectiveness=4), 4096)

    # Modifiers combine and round after each one. Reflect and Life Orb is 2662.5, which rounds down to 2662.
    def test_modifiers_stack(self):
        self.assertEqual(self.modifier(make_calc("snorlax", "machamp", "tackle", attacker_item="life-orb"), reflect=True), 2662)

    # Shadow Shield halves dmg at full HP, like Multiscale.
    def test_shadow_shield(self):
        calc = make_calc("garchomp", "lunala", "earthquake", defender_ability="shadow-shield")
        self.assertEqual(self.modifier(calc), 2048)
        self.assertEqual(self.modifier(calc, defender_hp=calc.defender_stats["hp"] - 1), 4096)

    # Solid Rock and Prism Armor work like Filter, and Punk Rock halves sound moves.
    def test_solid_rock_prism_armor_and_punk_rock(self):
        self.assertEqual(self.modifier(make_calc("lapras", "rhyperior", "surf", defender_ability="solid-rock"), physical=False, effectiveness=4), 3072)
        self.assertEqual(self.modifier(make_calc("lapras", "necrozma", "surf", defender_ability="prism-armor"), physical=False, effectiveness=2), 3072)
        self.assertEqual(self.modifier(make_calc("primarina", "toxtricity-amped", "boomburst", defender_ability="punk-rock"), physical=False), 2048)

class DamageCalcTests(TestCase):
    fixtures = ["pokedata"]

    # Calls calculate() and returns the lowest and highest dmg roll.
    def damage(self, calc, **options):
        result = calc.calculate(**options)
        return (result["min_damage"], result["max_damage"])

    # A level 50 Machamp's Close Combat on a level 50 Snorlax.
    def test_basic_damage(self):
        result = make_calc("machamp", "snorlax", "close-combat").calculate()
        self.assertEqual((result["min_damage"], result["max_damage"]), (240, 284))
        self.assertEqual(len(result["rolls"]), 16)
        self.assertEqual(result["ko_chance"], "Guaranteed OHKO")

    # 252 Atk Jolly Garchomp's Earthquake on 4 HP Heatran.
    def test_evs_and_nature(self):
        result = make_calc("garchomp", "heatran", "earthquake", attacker_build={"evs": {"hp": 4, "attack": 252, "speed": 252}, "nature": "jolly"}, defender_build={"evs": {"hp": 4}}).calculate()
        self.assertEqual((result["min_damage"], result["max_damage"]), (328, 388))

    # A crit at +2 Attack.
    def test_crit_and_boosts(self):
        result = make_calc("machamp", "snorlax", "close-combat").calculate(critical_hit=True, attacker_boosts={"attack": 2})
        self.assertEqual((result["min_damage"], result["max_damage"]), (716, 846))

    # Sun boosts Fire moves.
    def test_weather(self):
        result = make_calc("charizard", "venusaur", "flamethrower", weather="sun").calculate()
        self.assertEqual((result["min_damage"], result["max_damage"]), (168, 198))

    # Huge Power and Choice Band stack.
    def test_ability_and_item(self):
        result = make_calc("azumarill", "snorlax", "aqua-tail", attacker_ability="huge-power", attacker_item="choice-band").calculate()
        self.assertEqual((result["min_damage"], result["max_damage"]), (126, 148))

    # Multiscale halves the dmg at full HP.
    def test_multiscale(self):
        result = make_calc("dragonite", "dragonite", "outrage", attacker_ability="inner-focus", defender_ability="multiscale").calculate()
        self.assertEqual((result["min_damage"], result["max_damage"]), (91, 108))

    # A burned Pokémon's physical moves do half dmg.
    def test_burn(self):
        calc = DamageCalculator(Species.objects.get(name="snorlax"), Species.objects.get(name="machamp"), "normal", 85, "body-slam", "burn", None)
        result = calc.calculate()
        self.assertEqual((result["min_damage"], result["max_damage"]), (31, 37))

    # Skill Link makes Icicle Spear hit 5 times. Thick Fat halves each hit.
    def test_multi_hit(self):
        result = make_calc("cloyster", "snorlax", "icicle-spear", attacker_ability="skill-link", defender_ability="thick-fat").calculate()
        self.assertEqual(result["hits"], 5)
        self.assertEqual((result["min_damage"], result["max_damage"]), (50, 65))

    # Red Orb turns Groudon into Primal Groudon.
    def test_primal_groudon(self):
        result = make_calc("groudon", "snorlax", "precipice-blades", attacker_item="red-orb", weather="extreme-sun").calculate()
        self.assertEqual((result["min_damage"], result["max_damage"]), (160, 189))

    # Immunities do no dmg.
    def test_immunities(self):
        self.assertEqual(make_calc("snorlax", "gengar", "tackle").calculate()["max_damage"], 0)
        self.assertEqual(make_calc("garchomp", "heatran", "earthquake", defender_item="air-balloon").calculate()["max_damage"], 0)
        self.assertEqual(make_calc("garchomp", "rotom", "earthquake", defender_ability="levitate").calculate()["max_damage"], 0)

    # Seismic Toss does dmg equal to the user's level.
    def test_fixed_damage(self):
        result = make_calc("machamp", "snorlax", "seismic-toss", attacker_build={"level": 37}).calculate()
        self.assertEqual(result["rolls"], [37] * 16)

    # Status moves don't do dmg.
    def test_status_move(self):
        result = make_calc("machamp", "snorlax", "growl").calculate()
        self.assertEqual(result["max_damage"], 0)
        self.assertEqual(result["note"], "Status moves don't do damage.")

    # calculate() puts the calculator back how it was, so it can be run again.
    def test_calculate_twice(self):
        calc = make_calc("groudon", "snorlax", "precipice-blades", attacker_item="red-orb", weather="extreme-sun")
        first = calc.calculate()
        self.assertEqual(calc.attacker.name, "groudon")
        self.assertEqual(calc.calculate(), first)

    # Reflect halves physical dmg and Light Screen halves special dmg. Crits go through screens.
    def test_screens(self):
        reflect = make_calc("machamp", "snorlax", "close-combat").calculate(reflect=True)
        self.assertEqual((reflect["min_damage"], reflect["max_damage"]), (120, 142))
        light_screen = make_calc("charizard", "venusaur", "flamethrower").calculate(light_screen=True)
        self.assertEqual((light_screen["min_damage"], light_screen["max_damage"]), (55, 66))
        crit = make_calc("machamp", "snorlax", "close-combat").calculate(critical_hit=True, reflect=True)
        self.assertEqual((crit["min_damage"], crit["max_damage"]), (360, 426))

    # The % is out of the defender's max HP, but the KO chance goes by its current HP.
    def test_percent_and_ko_text(self):
        result = make_calc("garchomp", "snorlax", "earthquake").calculate()
        self.assertEqual((result["min_percent"], result["max_percent"]), (42.5, 50.2))
        self.assertEqual(result["ko_chance"], "Possible 2HKO")
        hurt = make_calc("garchomp", "snorlax", "earthquake").calculate(defender_hp=150)
        self.assertEqual((hurt["min_percent"], hurt["max_percent"]), (42.5, 50.2))
        self.assertEqual(hurt["ko_chance"], "Guaranteed 2HKO")

    # When only some rolls KO, it says the chance. 7 of the 16 rolls KO here.
    def test_ohko_chance(self):
        result = make_calc("gengar", "alakazam", "shadow-ball").calculate()
        self.assertEqual((result["min_damage"], result["max_damage"]), (116, 140))
        self.assertEqual(result["ko_chance"], "43.8% chance to OHKO")

    # Triple Axel hits 3 times with 20, 40 and 60 power.
    def test_triple_axel(self):
        result = make_calc("weavile", "garchomp", "triple-axel").calculate()
        self.assertEqual(result["hits"], 3)
        self.assertEqual((result["min_damage"], result["max_damage"]), (340, 412))

    # Moves that can't hit or can't be calculated yet say why in the note.
    def test_notes(self):
        self.assertEqual(make_calc("garchomp", "dragonite", "earthquake").calculate()["note"], "The move doesn't affect the target.")
        self.assertEqual(make_calc("machamp", "snorlax", "counter").calculate()["note"], "This move's damage can't be calculated yet.")
        self.assertIsNone(make_calc("machamp", "snorlax", "close-combat").calculate()["note"])

    # Adaptability makes STAB 2x instead of 1.5x. Technician boosts Bullet Punch to 60 power.
    def test_adaptability_and_technician(self):
        self.assertEqual(self.damage(make_calc("porygon-z", "snorlax", "hyper-beam", attacker_ability="adaptability")), (136, 160))
        result = make_calc("scizor", "gardevoir", "bullet-punch", attacker_ability="technician").calculate()
        self.assertEqual(result["base_power"], 60)
        self.assertEqual((result["min_damage"], result["max_damage"]), (120, 144))
        self.assertEqual(result["ko_chance"], "6.2% chance to OHKO")

    # 4x super effective and not very effective hits.
    def test_type_effectiveness(self):
        result = make_calc("lapras", "garchomp", "ice-beam").calculate()
        self.assertEqual(result["effectiveness"], 4)
        self.assertEqual((result["min_damage"], result["max_damage"]), (204, 244))
        resisted = make_calc("charizard", "blastoise", "flamethrower").calculate()
        self.assertEqual(resisted["effectiveness"], 0.5)
        self.assertEqual((resisted["min_damage"], resisted["max_damage"]), (26, 31))

    # Rain boosts Water moves and weakens Fire moves.
    def test_rain(self):
        surf = make_calc("blastoise", "charizard", "surf", weather="rain").calculate()
        self.assertEqual((surf["min_damage"], surf["max_damage"]), (152, 182))
        self.assertEqual(surf["ko_chance"], "93.8% chance to OHKO")
        self.assertEqual(self.damage(make_calc("charizard", "venusaur", "flamethrower", weather="rain")), (54, 66))

    # Electric Terrain boosts Electric moves and Grassy Terrain halves Earthquake.
    def test_terrain(self):
        electric = DamageCalculator(Species.objects.get(name="pikachu"), Species.objects.get(name="gyarados"), "electric", 90, "thunderbolt", None, None, terrain="electric-terrain")
        self.assertEqual(self.damage(electric), (160, 192))
        grassy = DamageCalculator(Species.objects.get(name="garchomp"), Species.objects.get(name="snorlax"), "ground", 100, "earthquake", None, None, terrain="grassy-terrain")
        self.assertEqual(self.damage(grassy), (51, 60))

    # Life Orb boosts everything, Expert Belt boosts super effective hits, and Sheer Force still gets the Life Orb boost.
    def test_life_orb_and_expert_belt(self):
        self.assertEqual(self.damage(make_calc("garchomp", "snorlax", "earthquake", attacker_item="life-orb")), (130, 153))
        self.assertEqual(self.damage(make_calc("garchomp", "heatran", "earthquake", attacker_item="expert-belt")), (322, 389))
        self.assertEqual(self.damage(make_calc("nidoking", "snorlax", "flamethrower", attacker_ability="sheer-force", attacker_item="life-orb")), (47, 56))

    # Yache Berry halves a super effective Ice move. On a multi-hit move it only halves the first hit.
    def test_resist_berries(self):
        self.assertEqual(self.damage(make_calc("lapras", "garchomp", "ice-beam", defender_item="yache-berry")), (102, 122))
        self.assertEqual(self.damage(make_calc("cloyster", "garchomp", "icicle-spear", attacker_ability="skill-link")), (320, 380))
        self.assertEqual(self.damage(make_calc("cloyster", "garchomp", "icicle-spear", attacker_ability="skill-link", defender_item="yache-berry")), (288, 342))

    # The defender's stat boosts lower the dmg.
    def test_defender_boosts(self):
        self.assertEqual(self.damage(make_calc("machamp", "snorlax", "close-combat"), defender_boosts={"defense": 1}), (162, 192))

    # Lower level, 0 IVs and a full EV spread on both sides all change the dmg.
    def test_level_ivs_and_evs(self):
        level_30 = make_calc("machamp", "snorlax", "close-combat", attacker_build={"level": 30}).calculate()
        self.assertEqual((level_30["min_damage"], level_30["max_damage"]), (96, 114))
        self.assertEqual(level_30["ko_chance"], "Guaranteed 3HKO")
        zero_ivs = make_calc("machamp", "snorlax", "close-combat", attacker_build={"ivs": {"hp": 0, "attack": 0, "defense": 0, "special-attack": 0, "special-defense": 0, "speed": 0}}).calculate()
        self.assertEqual((zero_ivs["min_damage"], zero_ivs["max_damage"]), (216, 254))
        self.assertEqual(zero_ivs["ko_chance"], "50.0% chance to OHKO")
        spread = make_calc("machamp", "snorlax", "close-combat", attacker_build={"evs": {"attack": 252}, "nature": "adamant"}, defender_build={"evs": {"hp": 252, "defense": 252}, "nature": "impish"})
        self.assertEqual(self.damage(spread), (212, 252))

    # Guts ignores the burn's dmg drop and boosts Attack. Facade doubles and also ignores the burn.
    def test_guts_and_facade(self):
        guts = DamageCalculator(Species.objects.get(name="machamp"), Species.objects.get(name="snorlax"), "fighting", 120, "close-combat", "burn", None, attacker_ability="guts")
        self.assertEqual(self.damage(guts), (356, 422))
        facade = DamageCalculator(Species.objects.get(name="snorlax"), Species.objects.get(name="machamp"), "normal", 70, "facade", "burn", None)
        self.assertEqual(self.damage(facade), (103, 123))

    # Assault Vest boosts Sp. Def and Eviolite boosts Chansey's Defense.
    def test_assault_vest_and_eviolite(self):
        self.assertEqual(self.damage(make_calc("gengar", "snorlax", "sludge-bomb", defender_item="assault-vest")), (40, 48))
        self.assertEqual(self.damage(make_calc("machamp", "chansey", "close-combat", defender_item="eviolite")), (548, 648))

    # Foul Play uses the target's Attack, Body Press uses Defense, and Psyshock hits Defense.
    def test_moves_that_use_other_stats(self):
        self.assertEqual(self.damage(make_calc("umbreon", "garchomp", "foul-play")), (70, 84))
        self.assertEqual(self.damage(make_calc("corviknight", "snorlax", "body-press")), (90, 106))
        self.assertEqual(self.damage(make_calc("alakazam", "blissey", "psyshock")), (232, 274))

    # Knock Off, Low Kick, Eruption and Gyro Ball get their power worked out before the dmg.
    def test_variable_power_moves(self):
        knock_off = make_calc("weavile", "alakazam", "knock-off", defender_item="leftovers").calculate()
        self.assertEqual((knock_off["base_power"], knock_off["min_damage"], knock_off["max_damage"]), (97, 236, 278))
        self.assertEqual(make_calc("machamp", "snorlax", "low-kick").calculate()["base_power"], 120)
        eruption = make_calc("typhlosion", "snorlax", "eruption").calculate(attacker_hp=50)
        self.assertEqual((eruption["base_power"], eruption["min_damage"], eruption["max_damage"]), (49, 28, 34))
        gyro_ball = make_calc("ferrothorn", "weavile", "gyro-ball").calculate()
        self.assertEqual((gyro_ball["base_power"], gyro_ball["min_damage"], gyro_ball["max_damage"]), (91, 138, 164))

    # Sandstorm boosts Tyranitar's Sp. Def.
    def test_sandstorm(self):
        self.assertEqual(self.damage(make_calc("gardevoir", "tyranitar", "moonblast", weather="sandstorm")), (86, 104))

    # Sacred Sword ignores the defender's Defense boosts and Unaware ignores the attacker's Attack boosts.
    def test_ignoring_stat_changes(self):
        self.assertEqual(self.damage(make_calc("gallade", "snorlax", "sacred-sword"), defender_boosts={"defense": 2}), (174, 206))
        self.assertEqual(self.damage(make_calc("machamp", "snorlax", "close-combat", defender_ability="unaware"), attacker_boosts={"attack": 2}), (240, 284))

    # Filter lowers super effective dmg and Fluffy doubles Fire dmg.
    def test_filter_and_fluffy(self):
        self.assertEqual(self.damage(make_calc("lapras", "garchomp", "ice-beam", defender_ability="filter")), (153, 183))
        self.assertEqual(self.damage(make_calc("charizard", "dubwool", "flamethrower", defender_ability="fluffy")), (120, 144))

    # Tinted Lens doubles a 1/4 effective hit and Sniper boosts crits.
    def test_tinted_lens_and_sniper(self):
        self.assertEqual(self.damage(make_calc("butterfree", "charizard", "bug-buzz", attacker_ability="tinted-lens")), (26, 32))
        self.assertEqual(self.damage(make_calc("kingdra", "snorlax", "surf", attacker_ability="sniper"), critical_hit=True), (103, 123))

    # Loaded Dice makes Bullet Seed hit 4 times.
    def test_loaded_dice(self):
        result = make_calc("cinccino", "snorlax", "bullet-seed", attacker_item="loaded-dice").calculate()
        self.assertEqual(result["hits"], 4)
        self.assertEqual((result["min_damage"], result["max_damage"]), (52, 64))

    # Spectral Thief steals the target's boosts, Meteor Beam raises Sp. Atk before hitting and Photon Geyser turns physical when Attack is higher.
    def test_stat_changing_moves(self):
        self.assertEqual(self.damage(make_calc("marshadow", "garchomp", "spectral-thief"), defender_boosts={"attack": 2}), (127, 151))
        self.assertEqual(self.damage(make_calc("gardevoir", "snorlax", "meteor-beam")), (76, 90))
        photon_geyser = make_calc("necrozma", "snorlax", "photon-geyser").calculate(attacker_boosts={"attack": 2})
        self.assertEqual(photon_geyser["category"], "physical")
        self.assertEqual((photon_geyser["min_damage"], photon_geyser["max_damage"]), (169, 199))

    # Freeze-Dry is super effective on Water types, Scrappy hits Ghost types with Normal moves and Mold Breaker ignores Levitate.
    def test_type_tricks(self):
        freeze_dry = make_calc("lapras", "gyarados", "freeze-dry").calculate()
        self.assertEqual(freeze_dry["effectiveness"], 4)
        self.assertEqual((freeze_dry["min_damage"], freeze_dry["max_damage"]), (136, 168))
        self.assertEqual(self.damage(make_calc("kangaskhan", "gengar", "body-slam", attacker_ability="scrappy")), (69, 82))
        self.assertEqual(self.damage(make_calc("excadrill", "rotom", "earthquake", attacker_ability="mold-breaker", defender_ability="levitate")), (182, 216))

    # Wonder Guard only lets super effective moves hit, and Fire moves fail in heavy rain.
    def test_moves_that_fail(self):
        blocked = make_calc("garchomp", "shedinja", "earthquake", defender_ability="wonder-guard").calculate()
        self.assertEqual(blocked["note"], "The move doesn't affect the target.")
        self.assertEqual(make_calc("garchomp", "shedinja", "fire-fang", defender_ability="wonder-guard").calculate()["ko_chance"], "Guaranteed OHKO")
        heavy_rain = make_calc("charizard", "snorlax", "flamethrower", weather="heavy-rain").calculate()
        self.assertEqual((heavy_rain["max_damage"], heavy_rain["note"]), (0, "The move doesn't affect the target."))

    # Battle Armor blocks crits and Frost Breath always crits.
    def test_crit_rules(self):
        blocked = make_calc("machamp", "snorlax", "close-combat", defender_ability="battle-armor").calculate(critical_hit=True)
        self.assertFalse(blocked["critical_hit"])
        self.assertEqual((blocked["min_damage"], blocked["max_damage"]), (240, 284))
        frost_breath = make_calc("lapras", "garchomp", "frost-breath").calculate()
        self.assertTrue(frost_breath["critical_hit"])
        self.assertEqual((frost_breath["min_damage"], frost_breath["max_damage"]), (208, 252))

    # Sword of Ruin lowers the target's Defense and Booster Energy boosts Great Tusk's Attack.
    def test_ruin_and_booster_energy(self):
        self.assertEqual(self.damage(make_calc("chien-pao", "garchomp", "icicle-crash")), (312, 372))
        self.assertEqual(self.damage(make_calc("great-tusk", "snorlax", "headlong-rush", attacker_item="booster-energy")), (156, 184))

    # Choice Specs Latios's Draco Meteor on Garchomp.
    def test_choice_specs(self):
        self.assertEqual(self.damage(make_calc("latios", "garchomp", "draco-meteor", attacker_item="choice-specs")), (314, 372))

    # Choice Band Dragonite's Outrage on Garchomp.
    def test_choice_band(self):
        self.assertEqual(self.damage(make_calc("dragonite", "garchomp", "outrage", attacker_item="choice-band")), (272, 324))

    # Kyogre's Water Spout in rain gets weaker as Kyogre loses HP.
    def test_water_spout_in_rain(self):
        full = make_calc("kyogre", "heatran", "water-spout", weather="rain").calculate()
        self.assertEqual((full["base_power"], full["min_damage"], full["max_damage"]), (150, 344, 408))
        hurt = make_calc("kyogre", "heatran", "water-spout", weather="rain").calculate(attacker_hp=100)
        self.assertEqual((hurt["base_power"], hurt["min_damage"], hurt["max_damage"]), (85, 198, 234))

    # Volcarona's Fiery Dance is 4x on Ferrothorn.
    def test_four_times_weak(self):
        result = make_calc("volcarona", "ferrothorn", "fiery-dance").calculate()
        self.assertEqual(result["effectiveness"], 4)
        self.assertEqual((result["min_damage"], result["max_damage"]), (208, 252))

    # Snow boosts Abomasnow's Defense, which turns a guaranteed OHKO into a 12.5% chance.
    def test_snow(self):
        self.assertEqual(make_calc("machamp", "abomasnow", "close-combat").calculate()["ko_chance"], "Guaranteed OHKO")
        snow = make_calc("machamp", "abomasnow", "close-combat", weather="snow").calculate()
        self.assertEqual((snow["min_damage"], snow["max_damage"]), (144, 170))
        self.assertEqual(snow["ko_chance"], "12.5% chance to OHKO")

    # Aurora Veil halves dmg like Reflect.
    def test_aurora_veil(self):
        self.assertEqual(self.damage(make_calc("machamp", "snorlax", "close-combat"), aurora_veil=True), (120, 142))

    # A level 1 Magikarp's Tackle still does at least 1 dmg.
    def test_minimum_damage(self):
        self.assertEqual(self.damage(make_calc("magikarp", "blissey", "tackle", attacker_build={"level": 1})), (1, 2))


# Tests for the dmg math helpers at the top of damage_calculator.py.
class MathHelperTests(TestCase):

    # .5 rounds down and anything over .5 rounds up.
    def test_poke_round(self):
        self.assertEqual(poke_round(2.5), 2)
        self.assertEqual(poke_round(2.51), 3)
        self.assertEqual(poke_round(3.4), 3)
        self.assertEqual(poke_round(7), 7)

    # Multipliers turn into modifiers out of 4096.
    def test_to_4096(self):
        self.assertEqual(to_4096(1.5), 6144)
        self.assertEqual(to_4096(1.3), 5325)
        self.assertEqual(to_4096(1.2), 4915)
        self.assertEqual(to_4096(0.5), 2048)

    # No modifiers means 4096 (1x).
    def test_chain_modifiers_empty(self):
        self.assertEqual(chain_modifiers([], 41, 131072), 4096)
        self.assertEqual(chain_modifiers([4096, 4096], 41, 131072), 4096)

    # Modifiers round after each one. Reflect and Life Orb is 2662.5, which rounds down.
    def test_chain_modifiers_rounding(self):
        self.assertEqual(chain_modifiers([6144], 41, 131072), 6144)
        self.assertEqual(chain_modifiers([2048, 5324], 41, 131072), 2662)

    # The combined modifier can't go below or above the limits.
    def test_chain_modifiers_limits(self):
        self.assertEqual(chain_modifiers([1], 41, 131072), 41)
        self.assertEqual(chain_modifiers([131072, 131072], 41, 131072), 131072)

    # Stat stages. +1 is 1.5x, +6 is 4x, -1 is 2/3 and -6 is 1/4.
    def test_apply_stage(self):
        self.assertEqual(apply_stage(150, 0), 150)
        self.assertEqual(apply_stage(150, 1), 225)
        self.assertEqual(apply_stage(150, 6), 600)
        self.assertEqual(apply_stage(150, -1), 100)
        self.assertEqual(apply_stage(150, -6), 37)

    # Stages past +6 or -6 count as +6 or -6.
    def test_apply_stage_caps(self):
        self.assertEqual(apply_stage(150, 8), 600)
        self.assertEqual(apply_stage(150, -8), 37)

    # Before and after values turn into a modifier. No change, a 0 or a None gives 4096.
    def test_modifier_from(self):
        self.assertEqual(modifier_from(150, 100), 6144)
        self.assertEqual(modifier_from(50, 100), 2048)
        self.assertEqual(modifier_from(100, 100), 4096)
        self.assertEqual(modifier_from(100, 0), 4096)
        self.assertEqual(modifier_from(None, 100), 4096)

    # No dmg, a guaranteed OHKO and a chance to OHKO.
    def test_ko_chance_one_hit(self):
        self.assertEqual(ko_chance_text([0] * 16, 100), "No damage")
        self.assertEqual(ko_chance_text([100] * 16, 100), "Guaranteed OHKO")
        self.assertEqual(ko_chance_text([90] * 12 + [100] * 4, 100), "25.0% chance to OHKO")

    # Guaranteed and possible multi-hit KOs.
    def test_ko_chance_more_hits(self):
        self.assertEqual(ko_chance_text([50] * 16, 100), "Guaranteed 2HKO")
        self.assertEqual(ko_chance_text(list(range(40, 56)), 100), "Possible 2HKO")

    # With no modifiers the 16 rolls go from 85% to 100% of the base dmg.
    def test_damage_rolls(self):
        self.assertEqual(damage_rolls(100, 4096, 1, False, 4096), list(range(85, 101)))

    # STAB, type effectiveness and burn change each roll, and every roll does at least 1.
    def test_damage_rolls_modifiers(self):
        self.assertEqual(damage_rolls(100, 6144, 1, False, 4096)[-1], 150)
        self.assertEqual(damage_rolls(100, 4096, 2, False, 4096)[-1], 200)
        self.assertEqual(damage_rolls(100, 4096, 1, True, 4096)[-1], 50)
        self.assertEqual(damage_rolls(1, 4096, 0.25, False, 4096), [1] * 16)


# Tests for the commands that import data from PokeAPI. requests.get is faked, so no real API calls are made.

STAT_NAMES = ["hp", "attack", "defense", "special-attack", "special-defense", "speed"]


# A fake PokeAPI response that returns the given JSON. Pass error=True to make raise_for_status fail like a 404 would.
def fake_response(data, error=False):
    response = Mock()
    response.json.return_value = data
    if error:
        response.raise_for_status.side_effect = requests.HTTPError("404 Not Found")
    return response


# Runs an import command with requests.get faked. fake(key) builds the normal response, special = {key: response} for ones that need real data.
# The key is the end of the URL, like "fire" or "25". Returns every (url, timeout) the command asked for.
def run_import(command, fake, special=None):
    special = special or {}
    with patch(f"pokedata.management.commands.{command}.requests.get") as get:
        get.side_effect = lambda url, timeout: special.get(url.rstrip("/").split("/")[-1]) or fake_response(fake(url.rstrip("/").split("/")[-1]))
        call_command(command)
    return [(call.args[0], call.kwargs["timeout"]) for call in get.call_args_list]


# A fake /type/ response. relations = {"double_damage_to": ["grass"], ...}. Anything left out is empty.
def fake_type(name, relations=None):
    keys = ["double_damage_from", "double_damage_to", "half_damage_from", "half_damage_to", "no_damage_from", "no_damage_to"]
    relations = relations or {}
    return {"name": name, "damage_relations": {key: [{"name": other, "url": ""} for other in relations.get(key, [])] for key in keys}}


# A fake /move/ response. Anything not given gets a plain default.
def fake_move(move_id, **changes):
    move = {
        "id": int(move_id), "name": f"move-{move_id}", "power": 50, "accuracy": 100, "priority": 0, "pp": 10, "effect_chance": None,
        "damage_class": {"name": "physical"}, "type": {"name": "normal"}, "stat_changes": [],
        "meta": {"ailment": {"name": "none"}, "category": {"name": "damage"}, "min_hits": None, "max_hits": None, "min_turns": None, "max_turns": None,
                 "drain": 0, "healing": 0, "crit_rate": 0, "ailment_chance": 0, "flinch_chance": 0, "stat_chance": 0},
    }
    move.update(changes)
    return move


# A fake /pokemon/ response. stats = the 6 base stats in order. moves = [(move name, [version groups])].
def fake_pokemon(pokedex_id, name=None, types=("normal",), abilities=("run-away",), stats=(50, 50, 50, 50, 50, 50), moves=()):
    return {
        "id": int(pokedex_id), "name": name or f"pokemon-{pokedex_id}", "height": 10, "weight": 100, "base_experience": 60,
        "types": [{"slot": slot, "type": {"name": type_name}} for slot, type_name in enumerate(types, 1)],
        "abilities": [{"ability": {"name": ability}} for ability in abilities],
        "stats": [{"base_stat": value, "stat": {"name": stat}} for stat, value in zip(STAT_NAMES, stats)],
        "sprites": {"other": {"official-artwork": {"front_default": f"https://example.com/{pokedex_id}.png"}}},
        "moves": [{"move": {"name": move}, "version_group_details": [{"version_group": {"name": group}} for group in groups]} for move, groups in moves],
    }


class ImportTypesTests(TestCase):

    # Runs the import once with real data for Fire. Each test checks what it saved.
    @classmethod
    def setUpTestData(cls):
        fire = fake_type("fire", {"double_damage_to": ["grass", "ice", "bug", "steel"], "half_damage_from": ["fire", "grass"], "double_damage_from": ["water", "ground", "rock"]})
        cls.requests_made = run_import("import_types", fake_type, {"fire": fake_response(fire)})

    # All 18 types get saved.
    def test_saves_all_types(self):
        self.assertEqual(Type.objects.count(), 18)
        self.assertTrue(Type.objects.filter(name="fairy").exists())

    # Each type's damage relations get saved as lists of type names.
    def test_damage_relations(self):
        type_data = Type.objects.get(name="fire").type_data
        self.assertEqual(type_data["double_damage_to"], ["grass", "ice", "bug", "steel"])
        self.assertEqual(type_data["double_damage_from"], ["water", "ground", "rock"])
        self.assertEqual(type_data["no_damage_to"], [])
        self.assertEqual(len(type_data), 6)

    # It asks PokeAPI for each type by name, with a 10 second timeout.
    def test_api_calls(self):
        self.assertEqual(len(self.requests_made), 18)
        self.assertIn(("https://pokeapi.co/api/v2/type/fire/", 10), self.requests_made)

    # Running it again doesn't make duplicates.
    def test_run_twice(self):
        run_import("import_types", fake_type)
        self.assertEqual(Type.objects.count(), 18)

    # If PokeAPI gives an error, the import stops there. Types saved before the error stay saved.
    def test_api_error(self):
        Type.objects.all().delete()
        with self.assertRaises(requests.HTTPError):
            run_import("import_types", fake_type, {"poison": fake_response({}, error=True)})
        self.assertEqual(sorted(Type.objects.values_list("name", flat=True)), ["fighting", "flying", "normal"])


class ImportMovesTests(TestCase):

    # Runs the import once with a few real moves mixed in. Each test checks what it saved.
    @classmethod
    def setUpTestData(cls):
        paralysis = {**fake_move(85)["meta"], "ailment": {"name": "paralysis"}, "category": {"name": "damage+ailment"}, "ailment_chance": 10}
        special = {
            "89": fake_response(fake_move(89, name="earthquake", power=100, type={"name": "ground"})),
            "14": fake_response(fake_move(14, name="swords-dance", power=None, accuracy=None, pp=20, damage_class={"name": "status"}, stat_changes=[{"change": 2, "stat": {"name": "attack"}}])),
            "98": fake_response(fake_move(98, name="quick-attack", power=40, priority=1, pp=30)),
            "85": fake_response(fake_move(85, name="thunderbolt", power=90, effect_chance=10, damage_class={"name": "special"}, meta=paralysis)),
            "331": fake_response(fake_move(331, name="bullet-seed", power=25, meta={**fake_move(331)["meta"], "min_hits": 2, "max_hits": 5})),
            "1": fake_response(fake_move(1, meta=None)),
            "2": fake_response(fake_move(2, meta={**fake_move(2)["meta"], "ailment": None})),
        }
        cls.requests_made = run_import("import_moves", fake_move, special)

    # All 919 moves get saved.
    def test_saves_all_moves(self):
        self.assertEqual(Moves.objects.count(), 919)
        self.assertEqual(Moves.objects.get(move_id=919).name, "move-919")

    # A move's type, category, power, accuracy, PP, priority and stat changes get saved.
    def test_move_fields(self):
        earthquake = Moves.objects.get(name="earthquake")
        self.assertEqual((earthquake.move_id, earthquake.type, earthquake.damage_class, earthquake.power, earthquake.accuracy, earthquake.pp), (89, "ground", "physical", 100, 100, 10))
        swords_dance = Moves.objects.get(name="swords-dance")
        self.assertEqual((swords_dance.damage_class, swords_dance.power, swords_dance.accuracy), ("status", None, None))
        self.assertEqual(swords_dance.stat_changes, [{"change": 2, "stat": {"name": "attack"}}])
        self.assertEqual(Moves.objects.get(name="quick-attack").priority, 1)

    # The move's meta (ailment, hits, drain, crit rate, etc.) gets flattened into plain values.
    def test_meta(self):
        thunderbolt = Moves.objects.get(name="thunderbolt")
        self.assertEqual((thunderbolt.meta["ailment"], thunderbolt.meta["category"], thunderbolt.meta["ailment_chance"], thunderbolt.effect_chance), ("paralysis", "damage+ailment", 10, 10))
        self.assertEqual(len(thunderbolt.meta), 12)
        bullet_seed = Moves.objects.get(name="bullet-seed")
        self.assertEqual((bullet_seed.meta["min_hits"], bullet_seed.meta["max_hits"]), (2, 5))

    # Moves with no meta (like Z-Moves) save None, and a missing ailment saves None.
    def test_missing_meta(self):
        self.assertIsNone(Moves.objects.get(move_id=1).meta)
        self.assertIsNone(Moves.objects.get(move_id=2).meta["ailment"])

    # It asks PokeAPI for each move by its number, with a 10 second timeout.
    def test_api_calls(self):
        self.assertEqual(len(self.requests_made), 919)
        self.assertIn(("https://pokeapi.co/api/v2/move/919/", 10), self.requests_made)

    # Running it again doesn't make duplicates.
    def test_run_twice(self):
        run_import("import_moves", fake_move)
        self.assertEqual(Moves.objects.count(), 919)


class ImportSpeciesTests(TestCase):

    # Runs the import once with real data for Charizard. Each test checks what it saved.
    @classmethod
    def setUpTestData(cls):
        charizard = fake_pokemon(6, "charizard", types=("fire", "flying"), abilities=("blaze", "solar-power"), stats=(78, 84, 78, 109, 85, 100))
        cls.requests_made = run_import("import_species", fake_pokemon, {"6": fake_response(charizard)})

    # All 1025 Pokémon get saved.
    def test_saves_all_species(self):
        self.assertEqual(Species.objects.count(), 1025)
        self.assertEqual(Species.objects.get(pokedex_id=1025).name, "pokemon-1025")

    # Types, abilities, base stats, size and sprite get saved. Charizard's base stats add up to 534.
    def test_species_fields(self):
        charizard = Species.objects.get(name="charizard")
        self.assertEqual((charizard.pokedex_id, charizard.types, charizard.abilities), (6, ["fire", "flying"], ["blaze", "solar-power"]))
        self.assertEqual(charizard.stats, {"hp": 78, "attack": 84, "defense": 78, "special-attack": 109, "special-defense": 85, "speed": 100})
        self.assertEqual(charizard.total_stats, 534)
        self.assertEqual((charizard.height, charizard.weight, charizard.base_experience), (10, 100, 60))
        self.assertEqual(charizard.sprite, "https://example.com/6.png")

    # It asks PokeAPI for each Pokémon by its pokedex number, with a 10 second timeout.
    def test_api_calls(self):
        self.assertEqual(len(self.requests_made), 1025)
        self.assertIn(("https://pokeapi.co/api/v2/pokemon/25/", 10), self.requests_made)

    # Running it again doesn't make duplicates.
    def test_run_twice(self):
        run_import("import_species", fake_pokemon)
        self.assertEqual(Species.objects.count(), 1025)

    # If PokeAPI gives an error, the import stops there. Pokémon saved before the error stay saved.
    def test_api_error(self):
        Species.objects.all().delete()
        with self.assertRaises(requests.HTTPError):
            run_import("import_species", fake_pokemon, {"4": fake_response({}, error=True)})
        self.assertEqual(Species.objects.count(), 3)


class ImportLearnsetTests(TestCase):

    # Runs the import once with real moves for Pikachu and Garchomp. Each test checks what it saved.
    @classmethod
    def setUpTestData(cls):
        pikachu = fake_pokemon(25, "pikachu", moves=[("thunderbolt", ["scarlet-violet", "sword-shield"]), ("volt-tackle", ["scarlet-violet"]), ("thunder-punch", ["sword-shield"])])
        garchomp = fake_pokemon(445, "garchomp", moves=[("earthquake", ["scarlet-violet", "scarlet-violet"]), ("dragon-claw", ["scarlet-violet"])])
        cls.requests_made = run_import("import_learnset", fake_pokemon, {"25": fake_response(pikachu), "445": fake_response(garchomp)})

    # A learnset gets saved for all 1025 Pokémon.
    def test_saves_all_learnsets(self):
        self.assertEqual(Learnset.objects.count(), 1025)
        self.assertEqual(Learnset.objects.get(species_id=25).name, "pikachu")

    # Only moves learnable in Scarlet and Violet get saved.
    def test_scarlet_violet_only(self):
        self.assertEqual(sorted(Learnset.objects.get(species_id=25).moves), ["thunderbolt", "volt-tackle"])

    # A move learned more than one way in Scarlet and Violet (level up and TM) only shows up once.
    def test_no_duplicate_moves(self):
        self.assertEqual(sorted(Learnset.objects.get(species_id=445).moves), ["dragon-claw", "earthquake"])

    # A Pokémon with no Scarlet and Violet moves gets an empty learnset.
    def test_empty_learnset(self):
        self.assertEqual(Learnset.objects.get(species_id=1).moves, [])

    # It asks PokeAPI for each Pokémon by its pokedex number, with a 10 second timeout.
    def test_api_calls(self):
        self.assertEqual(len(self.requests_made), 1025)
        self.assertIn(("https://pokeapi.co/api/v2/pokemon/1/", 10), self.requests_made)

    # Running it again doesn't make duplicates.
    def test_run_twice(self):
        run_import("import_learnset", fake_pokemon)
        self.assertEqual(Learnset.objects.count(), 1025)


# Tests for the damage calculator page and the endpoint that fills in its dropdowns.
class CalcPageTests(TestCase):
    fixtures = ["pokedata"]

    # Loads the calc page with the given form values.
    def get(self, **values):
        return self.client.get(reverse("damage_calculator"), values)

    # With nothing picked, the page shows Garchomp's Earthquake on Snorlax.
    def test_default_matchup(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        result = response.context["result"]
        self.assertEqual((result["min_damage"], result["max_damage"]), (100, 118))
        self.assertContains(response, "Possible 2HKO")

    # The page gives the exact same result as calling calculate() directly.
    def test_matches_calculate(self):
        response = self.get(attacker_pokemon="machamp", move="close-combat", defender_pokemon="snorlax")
        self.assertEqual(response.context["result"], make_calc("machamp", "snorlax", "close-combat").calculate())

    # EVs and natures from the form go into the builds.
    def test_evs_and_nature(self):
        response = self.get(attacker_pokemon="garchomp", move="earthquake", defender_pokemon="heatran", attacker_ev_hp=4, attacker_ev_atk=252, attacker_ev_spe=252, attacker_nature="jolly", defender_ev_hp=4)
        result = response.context["result"]
        self.assertEqual((result["min_damage"], result["max_damage"]), (328, 388))

    # Stat stages and crits from the form.
    def test_boosts_and_crit(self):
        response = self.get(attacker_pokemon="machamp", move="close-combat", defender_pokemon="snorlax", attacker_boost_atk=2, critical_hit="on")
        result = response.context["result"]
        self.assertEqual((result["min_damage"], result["max_damage"]), (716, 846))

    # Weather, screens, abilities and items from the form.
    def test_field_abilities_and_items(self):
        sun = self.get(attacker_pokemon="charizard", move="flamethrower", defender_pokemon="venusaur", weather="sun").context["result"]
        self.assertEqual((sun["min_damage"], sun["max_damage"]), (168, 198))
        reflect = self.get(attacker_pokemon="machamp", move="close-combat", defender_pokemon="snorlax", reflect="on").context["result"]
        self.assertEqual((reflect["min_damage"], reflect["max_damage"]), (120, 142))
        band = self.get(attacker_pokemon="azumarill", move="aqua-tail", defender_pokemon="snorlax", attacker_ability="huge-power", attacker_item="choice-band").context["result"]
        self.assertEqual((band["min_damage"], band["max_damage"]), (126, 148))

    # Current HP % sets the defender's HP. Half of Snorlax's 235 HP is 117, so Earthquake now has a chance to OHKO.
    def test_hp_percent(self):
        result = self.get(defender_hp_percent=50).context["result"]
        self.assertEqual(result["defender_hp"], 117)
        self.assertEqual(result["ko_chance"], "12.5% chance to OHKO")

    # The summary line reads like Smogon's, with EVs, nature, item, level, stat stages, weather and crits.
    def test_description(self):
        band = self.get(attacker_pokemon="garchomp", move="earthquake", defender_pokemon="heatran", attacker_ev_atk=252, attacker_nature="adamant", attacker_item="choice-band", defender_ev_hp=4)
        self.assertEqual(band.context["description"], "252+ Atk Choice Band Garchomp Earthquake vs. 4 HP / 0 Def Heatran: 540-636 (323.3 - 380.8%) -- Guaranteed OHKO")
        sun = self.get(attacker_pokemon="charizard", move="flamethrower", defender_pokemon="venusaur", weather="sun", critical_hit="on", attacker_boost_spa=1, attacker_level=30)
        self.assertEqual(sun.context["description"], "+1 0 SpA Lvl 30 Charizard Flamethrower vs. 0 HP / 0 SpD Venusaur in Sun on a critical hit: 146-174 (94.1 - 112.2%) -- 68.8% chance to OHKO")

    # Pokémon, moves and items that don't exist show an error instead of a result.
    def test_bad_names(self):
        for values in ({"attacker_pokemon": "notamon"}, {"move": "growl"}, {"attacker_item": "banana"}):
            response = self.get(**values)
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.context["result"])
            self.assertTrue(response.context["form"].error_list())

    # EVs over 252, more than 510 total EVs, levels over 50 and abilities the Pokémon can't have show errors.
    def test_bad_builds(self):
        self.assertIn("Attacker Spe EVs: Ensure this value is less than or equal to 252.", self.get(attacker_ev_spe=300).context["form"].error_list())
        self.assertIn("Attacker: EVs can't add up to more than 510.", self.get(attacker_ev_hp=252, attacker_ev_atk=252, attacker_ev_spe=252).context["form"].error_list())
        self.assertIn("Attacker level: Ensure this value is less than or equal to 50.", self.get(attacker_level=60).context["form"].error_list())
        self.assertIn("Attacker ability: Garchomp can't have Levitate.", self.get(attacker_ability="levitate").context["form"].error_list())

    # The options endpoint gives a Pokémon's abilities and only the moves it learns that do damage.
    def test_pokemon_options(self):
        data = self.client.get(reverse("pokemon_options", args=["garchomp"])).json()
        self.assertEqual([ability["name"] for ability in data["abilities"]], ["sand-veil", "rough-skin"])
        moves = [move["name"] for move in data["moves"]]
        self.assertIn("earthquake", moves)
        self.assertNotIn("swords-dance", moves)
        self.assertEqual(moves, sorted(moves))

    # The options endpoint 404s for a Pokémon that doesn't exist.
    def test_pokemon_options_unknown(self):
        self.assertEqual(self.client.get(reverse("pokemon_options", args=["notamon"])).status_code, 404)

    # The nav bar links to the calc page.
    def test_nav_link(self):
        self.assertContains(self.client.get(reverse("home")), f'href="{reverse("damage_calculator")}"')


# Tests for the home page and the nav bar.
class HomePageTests(TestCase):
    fixtures = ["pokedata"]

    # The home page shows how many Pokémon, moves and held items there are.
    def test_counts(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual((response.context["species_count"], response.context["moves_count"], response.context["items_count"]), ("1,025", "919", "218"))

    # The home page links to the damage calculator and the Pokédex.
    def test_links(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, f'href="{reverse("damage_calculator")}"')
        self.assertContains(response, f'href="{reverse("pokemon_table")}"')

        # The home page and nav bar link to the team builder.
    def test_team_builder_link(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, f'href="{reverse("team_builder")}"', count=2)
        self.assertNotContains(response, "Coming soon")


class HealthCheckTests(TestCase):
    # The health check answers even when the Host header isn't in ALLOWED_HOSTS, like Lightsail's load balancer.
    def test_health_check_any_host(self):
        response = self.client.get("/health/", HTTP_HOST="10.0.0.5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    # Every other page still turns away hosts that aren't in ALLOWED_HOSTS.
    def test_other_pages_check_host(self):
        response = self.client.get("/", HTTP_HOST="10.0.0.5")
        self.assertEqual(response.status_code, 400)


class AdminSiteTests(TestCase):
    # Reloads the URLs, since urls.py only checks DEBUG when it's first loaded.
    def reload_urls(self):
        importlib.reload(config.urls)
        clear_url_caches()

    # The admin site is turned off when DEBUG is off, like on the live site.
    def test_admin_off_without_debug(self):
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 404)

    # The admin site is on when DEBUG is on, like when running it locally.
    def test_admin_on_with_debug(self):
        self.addCleanup(self.reload_urls)
        with self.settings(DEBUG=True):
            self.reload_urls()
            response = self.client.get("/admin/login/")
        self.assertEqual(response.status_code, 200)


class NameFormatTests(TestCase):
    fixtures = ["pokedata"]

    # pretty turns database names into names people read, and slug turns them back.
    def test_pretty_and_slug(self):
        self.assertEqual(pretty("choice-scarf"), "Choice Scarf")
        self.assertEqual(slug("  Choice Scarf "), "choice-scarf")
        self.assertEqual((pretty(""), slug(None)), ("", ""))

    # Every Pokémon, move and item name turns back into the same database name, so typing what the page shows always works.
    def test_every_name_round_trips(self):
        names = list(Species.objects.values_list("name", flat=True)) + list(Moves.objects.values_list("name", flat=True)) + ITEMS
        self.assertEqual([name for name in names if slug(pretty(name)) != name], [])

    # The calc page shows readable names in its boxes and name lists.
    def test_calc_shows_readable_names(self):
        response = self.client.get(reverse("damage_calculator"), {"attacker_item": "choice-band"})
        self.assertContains(response, 'value="Garchomp"')
        self.assertContains(response, 'value="Choice Band"')
        self.assertContains(response, '<option value="Iron Valiant">')
        self.assertNotContains(response, '<option value="iron-valiant">')

    # The calc gives the same result with readable names and database names.
    def test_calc_takes_readable_names(self):
        readable = self.client.get(reverse("damage_calculator"), {"attacker_pokemon": "Garchomp", "attacker_item": "Choice Band", "defender_pokemon": "Iron Valiant"})
        database = self.client.get(reverse("damage_calculator"), {"attacker_pokemon": "garchomp", "attacker_item": "choice-band", "defender_pokemon": "iron-valiant"})
        self.assertIsNotNone(readable.context["result"])
        self.assertEqual(readable.context["result"], database.context["result"])

    # Calc errors show names the way they were typed.
    def test_calc_error_names(self):
        errors = self.client.get(reverse("damage_calculator"), {"attacker_pokemon": "Notamon", "defender_item": "Rare Candy"}).context["form"].error_list()
        self.assertIn("Attacker Pokémon: Notamon isn't a Pokémon.", errors)
        self.assertIn("Defender item: Rare Candy isn't an item the calc knows.", errors)

    # The team builder shows readable names in its boxes and name list.
    def test_team_shows_readable_names(self):
        response = self.client.get(reverse("team_builder"), {"pokemon_1": "iron-valiant"})
        self.assertContains(response, 'value="Iron Valiant"')
        self.assertContains(response, '<option value="Iron Valiant">')

    # The team builder takes names with spaces, like "Iron Valiant".
    def test_team_takes_readable_names(self):
        response = self.client.get(reverse("team_builder"), {"pokemon_1": "Iron Valiant"})
        self.assertEqual(response.context["slots"][0]["species"].name, "iron-valiant")

    # The Pokédex shows readable names but keeps the database name for its links.
    def test_pokedex_names(self):
        response = self.client.get(reverse("pokemon_table"))
        self.assertContains(response, '<div class="pokemon-name">Iron Valiant</div>')
        self.assertContains(response, 'data-name="iron-valiant"')

    # The moves page shows readable move names and categories.
    def test_moves_page_names(self):
        response = self.client.get(reverse("moves"))
        self.assertContains(response, '<td class="move-name">Close Combat</td>')
        self.assertContains(response, "<td>Physical</td>")
        self.assertNotContains(response, ">close-combat<")

    # The name boxes are marked so the page's links use database names and stay short.
    def test_name_boxes_marked_for_links(self):
        for page, boxes in (("damage_calculator", 4), ("team_builder", 6)):
            html = self.client.get(reverse(page)).content.decode()
            self.assertEqual(len(re.findall(r"<input[^>]*data-slug", html)), boxes)


class TeamBuilderTests(TestCase):
    fixtures = ["pokedata"]

    # Loads the team builder with the given slots filled in.
    def get(self, **slots):
        return self.client.get(reverse("team_builder"), slots)

    # The names in each slot, or None for empty slots.
    def team_names(self, response):
        return [slot["species"].name if slot["species"] else None for slot in response.context["slots"]]

    # With nothing picked, the page shows six empty slots.
    def test_empty_team(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.team_names(response), [None] * 6)
        self.assertContains(response, "0 / 6 Pokémon")
        self.assertContains(response, "Empty slot", count=6)

    # Picked Pokémon show up in their slots.
    def test_picks_pokemon(self):
        response = self.get(pokemon_1="garchomp", pokemon_2="snorlax")
        self.assertEqual(self.team_names(response), ["garchomp", "snorlax", None, None, None, None])
        self.assertContains(response, "2 / 6 Pokémon")

    # A full team of six.
    def test_full_team(self):
        team = ["garchomp", "snorlax", "pikachu", "gengar", "dragonite", "tyranitar"]
        response = self.get(**{f"pokemon_{slot}": name for slot, name in enumerate(team, start=1)})
        self.assertEqual(self.team_names(response), team)
        self.assertContains(response, "6 / 6 Pokémon")
        self.assertNotContains(response, "Empty slot")

    # Slots can be left empty in between.
    def test_gaps(self):
        response = self.get(pokemon_3="pikachu", pokemon_6="gengar")
        self.assertEqual(self.team_names(response), [None, None, "pikachu", None, None, "gengar"])

    # Names work with any capitals and extra spaces.
    def test_name_cleanup(self):
        response = self.get(pokemon_1="  GarChomp ")
        self.assertEqual(self.team_names(response)[0], "garchomp")

    # The boxes keep the team, with names the way people read them.
    def test_keeps_typed_names(self):
        response = self.get(pokemon_1="garchomp")
        self.assertContains(response, 'value="Garchomp"')

    # A name that isn't a Pokémon shows an error, and the rest of the team still shows.
    def test_unknown_pokemon(self):
        response = self.get(pokemon_1="garchomp", pokemon_2="notamon")
        self.assertContains(response, "Notamon isn&#x27;t a Pokémon.")
        self.assertEqual(self.team_names(response)[:2], ["garchomp", None])
        self.assertContains(response, "1 / 6 Pokémon")

    # Like in real formats, the same Pokémon can't be on the team twice.
    def test_no_duplicates(self):
        response = self.get(pokemon_1="garchomp", pokemon_4="garchomp")
        self.assertContains(response, "Garchomp is already on your team.")
        self.assertEqual(self.team_names(response), ["garchomp", None, None, None, None, None])

    # Each Pokémon shows its types and base stats.
    def test_types_and_stats(self):
        response = self.get(pokemon_1="garchomp")
        self.assertContains(response, "type-badge type-dragon")
        self.assertContains(response, "type-badge type-ground")
        stats = {stat["label"]: stat["value"] for stat in response.context["slots"][0]["stats"]}
        self.assertEqual(stats, {"HP": 108, "Atk": 130, "Def": 95, "SpA": 80, "SpD": 85, "Spe": 102})
        self.assertContains(response, "Base stat total: 600")

    # A team only has six slots, so anything past slot 6 is ignored.
    def test_only_six_slots(self):
        response = self.get(pokemon_7="pikachu")
        self.assertEqual(self.team_names(response), [None] * 6)
        self.assertContains(response, "0 / 6 Pokémon")