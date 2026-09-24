Bug fixes
- drop, disapear before we have time to click on a cell of the earth.

Visuals
- glitch one card in hand continously showing the animation of arriving from deck to hand

AI

I want to add 2 new features:

1. A system that checks a loaded deck regarding the deck rules. The rules:
 - Exactly 20 cards from only one main faction
 - Singleton format: must only contain one copy of a card
 - A maximum of 5 cards sharing the same condition
    → temp_inf_6 and temp_inf_11 are considered same conditions
    → temp_sup_9 and temp_sup_15 are considered same conditions
    → dist_ahead_sup_1 and dist_ahead_sup_3 are considered same conditions
    → dist_behind_sup_1 and dist_behind_sup_3 are considered same conditions
 - maximum of 5 cards sharing the same effect
    → all number variants of an effect (ex.: advancing +1, +2 or +3) are considered same effect
 - refuse face_point_left and face_point_right for now (maybe allowed later, not implemented yet in online game)

2. I want to create a system that automatically creates a not so dumb deck for the random faction chosen by the AI system, it must of course specialy take into account the random support faction chosen. I still want randomness in the chosen cards:

if support faction is Engineers:
 → always add 5 cards with the condition drop_on_board 
 → do not add pending conditions in the deck
 → do not add temp_sup_15 or temp_inf_6 conditions
 → do not add draw effect (the draw will come from rafinery dweling card)

if support faction is Mages:
 → do not add drop_on_board conditions cards
 → do not add pending conditions in the deck
 → regarding the accessible conditions of the main faction add 2 to 4 temp_sup_15 or temp_inf_6 condition cards
 → regarding the accessible conditions of the main faction add 4 day or night condition cards
 → also add 4 cards of their biomes condition

if support faction is Doctors:
 → do not add drop_on_board conditions cards
 → always add 5 cards with the condition pending
 → do not add temp_sup_15 or temp_inf_6 conditions

General instructions:
 → always add 2 wrecking_ball effect cards
 → always add 2 jump effect cards
 → always add 2 to 4 draw cards (exept if support faction is Engineers)
 → always add 2 to 4 special main faction effect cards (Dwarves: avalanche, Demons: effect_canceled,  Twigs: rooted, Miaous: pet_trap, Orcs: swap_cards, Mummies: copy_effect)

for Mummies:
 → always add some mana_sup_5_oppo (or mana_inf_6_oppo) and cards_in_hand_sup_3_oppo (or cards_in_hand_inf_4_oppo) conditions cards into the deck

for Dwarves:
 → always add some block condition cards

if the combination of rules is not feasable, do the following priotiies: 1. support faction 2. General instructions 3. per faction rules

Game Analysis
- support faction cards not shown
- add instant cards report in the game analysis like in the ingame log system