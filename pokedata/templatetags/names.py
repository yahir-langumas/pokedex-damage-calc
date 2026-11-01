from django import template
from pokedata.forms import pretty

register = template.Library()


# Lets templates show database names the way people read them, like {{ move.name|pretty }} for "Close Combat".
@register.filter(name="pretty")
def pretty_filter(name):
    return pretty(name)