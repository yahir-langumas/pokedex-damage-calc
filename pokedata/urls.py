from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('pokemon-table/', views.pokemon_table, name='pokemon_table'),
    path('team-builder/', views.team_builder, name='team_builder'),
    path('team-builder/matchups/', views.team_matchups, name='team_matchups'),
    path('moves/', views.moves, name='moves'),
    path('damage-calculator/', views.damage_calculator, name='damage_calculator'),
    path('damage-calculator/pokemon/<str:name>/', views.pokemon_options, name='pokemon_options'),
]