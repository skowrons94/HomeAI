"""Built-in food tables: kcal/100 g (+ density, grams per piece) and substitutions.

Names are matched after accent/plural stripping (see ``parse.norm_key``), IT + EN.
KCAL line format: ``names | separated ; kcal per 100 g [; g per ml [; g per piece]]``.
Values are for the raw/dry ingredient as written in recipes.
"""

KCAL = """
spaghetti|penne|rigatoni|fusilli|farfalle|tagliatelle|linguine|bucatini|maccheroni|paccheri|pasta|macaroni|noodles|pasta secca;355;0.55
pasta fresca|fresh pasta|lasagne|lasagna sheets|pasta sfoglia;270
riso|rice|riso arborio|carnaroli|basmati;350;0.85
farina|flour|farina 00|farina 0|plain flour|all-purpose flour|self-raising flour|farina integrale|farina manitoba|bread flour;350;0.55
semola|semolina|semola rimacinata;360;0.65
fecola|amido di mais|maizena|cornflour|cornstarch|fecola di patate;350;0.6
pane|bread|pangrattato|breadcrumbs|pan grattato|panko;300;0.4;
pane in cassetta|sliced bread;265;;30
avena|fiocchi d'avena|oats|rolled oats|porridge oats;375;0.35
zucchero|sugar|caster sugar|zucchero semolato|granulated sugar|zucchero di canna|brown sugar|muscovado;390;0.85
zucchero a velo|icing sugar|powdered sugar|confectioners sugar;390;0.55
miele|honey|sciroppo d'acero|maple syrup|golden syrup;310;1.4
sale|salt|pepe|pepper|black pepper|pepe nero|spezie|spices|origano|oregano|paprika|cumin|cumino|cannella|cinnamon;0;1
olio|olio d'oliva|olio extravergine|olio extravergine di oliva|olive oil|extra virgin olive oil|vegetable oil|olio di semi|sunflower oil|olio di girasole|olio di arachidi|oil|rapeseed oil|coconut oil|olio di cocco;884;0.92
burro|butter|unsalted butter|margarina|margarine;740;0.95
strutto|lard;900;0.92
panna|cream|double cream|heavy cream|panna da cucina|panna fresca|single cream|whipping cream|panna montata;300;1
panna acida|sour cream|creme fraiche;190;1
latte|milk|whole milk|latte intero|latte scremato|skimmed milk|semi-skimmed milk;50;1.03
latte condensato|condensed milk;320;1.3
yogurt|yoghurt|greek yogurt|yogurt greco|natural yogurt;65;1.03
buttermilk;40;1.03
mascarpone;430
ricotta;150
mozzarella|fior di latte|bufala;250;;125
parmigiano|parmesan|parmigiano reggiano|grana|grana padano|parmigiano grattugiato|grated parmesan;400;0.45
pecorino|pecorino romano|pecorino grattugiato;390;0.45
cheddar|formaggio|cheese|gruyere|emmental|fontina|provolone|asiago|taleggio|gorgonzola|scamorza;380
feta|formaggio fresco|cream cheese|philadelphia|formaggio spalmabile|robiola|stracchino;250
uova|uovo|egg|eggs|large egg|medium egg;143;;55
tuorlo|tuorli|egg yolk|egg yolks|yolk|yolks;322;;18
albume|albumi|egg white|egg whites|white|whites;52;;33
guanciale|pancetta|bacon|streaky bacon|pancetta affumicata|smoked bacon|lardo|lardons;540
prosciutto crudo|prosciutto|parma ham|cured ham|speck|prosciutto cotto|ham|bresaola;230
salsiccia|sausage|sausages|italian sausage|salame|salami|chorizo|nduja;320;;80
pollo|chicken|petto di pollo|chicken breast|cosce di pollo|chicken thigh|chicken thighs|coscia di pollo;165;;150
tacchino|turkey|fesa di tacchino;150
manzo|beef|macinato|carne macinata|mince|minced beef|ground beef|beef mince|macinato di manzo|bistecca|steak;250
maiale|pork|lonza|pork loin|pork mince|macinato di maiale|costine|ribs;240
agnello|lamb|vitello|veal;230
tonno|tuna|tonno sott'olio|tuna in oil|tuna chunks;190
tonno al naturale|tuna in water;110
salmone|salmon|salmone affumicato|smoked salmon;200;;150
merluzzo|cod|baccala|branzino|sea bass|orata|fish fillet|white fish|pesce|nasello|haddock;90
gamberi|gamberetti|prawns|shrimp|king prawns|scampi;85
cozze|mussels|vongole|clams|calamari|squid|polpo|octopus|seppie;80
acciughe|acciuga|alici|anchovies|anchovy|anchovy fillets|filetti di acciughe;210
patate|patata|potato|potatoes|new potatoes;77;;150
cipolla|cipolle|onion|onions|red onion|cipolla rossa|cipollotto|scalogno|shallot|shallots|spring onion|spring onions|leek|porro|porri;40;;110
aglio|spicchio d'aglio|garlic|garlic clove|garlic cloves|clove of garlic|cloves garlic;130;;5
carota|carote|carrot|carrots;41;;80
sedano|celery|finocchio|fennel;20;;60
pomodoro|pomodori|tomato|tomatoes|pomodorini|cherry tomatoes|pomodori pelati|pelati|chopped tomatoes|tinned tomatoes|canned tomatoes|polpa di pomodoro|passata|passata di pomodoro|tomato passata|sugo|tomato sauce|salsa di pomodoro;25;1.03;120
concentrato di pomodoro|tomato puree|tomato paste|doppio concentrato|tomato puree paste;80;1.1
zucchina|zucchine|courgette|courgettes|zucchini;17;;200
melanzana|melanzane|aubergine|eggplant;25;;250
peperone|peperoni|pepper|bell pepper|red pepper|green pepper|peperoncino|chilli|chili|chillies|red chilli;30;;150
spinaci|spinach|cavolo|cabbage|broccoli|cavolfiore|cauliflower|verza|kale|cavolo nero|bietola|chard|rucola|rocket|arugula|lattuga|lettuce|insalata|salad leaves|zucca|pumpkin|butternut squash|asparagi|asparagus|carciofi|artichokes|cetriolo|cucumber|funghi|mushrooms|champignon|porcini|fagiolini|green beans|piselli|peas|frozen peas|mais|sweetcorn|corn;30
legumi|ceci|chickpeas|fagioli|beans|kidney beans|cannellini|borlotti|lenticchie|lentils|red lentils|black beans|fave|broad beans;340
ceci in scatola|fagioli in scatola|canned chickpeas|tinned beans|tinned chickpeas|tinned cannellini beans|butter beans;120
limone|limoni|lemon|lemons;29;;100
succo di limone|lemon juice|succo d'arancia|orange juice|lime juice|succo di lime;25;1.03
arancia|arance|orange|oranges|lime|limes|mandarino|clementine;45;;150
mela|mele|apple|apples|pera|pere|pear|pears|pesca|pesche|peach|peaches;52;;170
banana|banane|bananas;89;;120
fragole|strawberries|lamponi|raspberries|mirtilli|blueberries|frutti di bosco|berries|ciliegie|cherries|uva|grapes;45
uvetta|raisins|sultanas|datteri|dates|fichi secchi|dried figs|albicocche secche|dried apricots|prugne secche|prunes;290
mandorle|almonds|noci|walnuts|nocciole|hazelnuts|pistacchi|pistachios|pinoli|pine nuts|anacardi|cashews|arachidi|peanuts|frutta secca|nuts|semi di sesamo|sesame seeds|semi di girasole|sunflower seeds|cocco rapé|desiccated coconut|cocco;620
burro di arachidi|peanut butter|tahini;600;1
cioccolato|chocolate|cioccolato fondente|dark chocolate|cioccolato al latte|milk chocolate|gocce di cioccolato|chocolate chips|nutella;540
cacao|cocoa|cacao amaro|cocoa powder|cacao in polvere;230;0.5
lievito|lievito per dolci|baking powder|baking soda|bicarbonato|bicarbonate of soda|lievito di birra|yeast|dried yeast|lievito secco;0;;
vaniglia|vanilla|vanilla extract|estratto di vaniglia|vanillina|bustina di vanillina|aroma;0
gelatina|gelatin|colla di pesce|gelatine leaves;335
savoiardi|ladyfingers|biscotti|biscuits|digestive biscuits|biscotti secchi|amaretti|cookies|oreo|pan di stelle;430;;8
pasta sfoglia|puff pastry|pasta brisee|shortcrust pastry|pasta frolla|pizza dough|impasto per pizza|filo pastry;400
pangrattato tostato|crackers|grissini|taralli|fette biscottate;410
vino|wine|vino bianco|white wine|vino rosso|red wine|prosecco|marsala|sherry|brandy|rum|whisky|birra|beer|cognac|liquore;85;0.99
brodo|stock|broth|chicken stock|beef stock|vegetable stock|dado|stock cube|brodo vegetale|brodo di carne|bouillon;5;1
acqua|water|ghiaccio|ice;0;1
aceto|vinegar|aceto balsamico|balsamic vinegar|aceto di vino|white wine vinegar|red wine vinegar|aceto di mele|cider vinegar;20;1
senape|mustard|dijon mustard|maionese|mayonnaise|ketchup|salsa di soia|soy sauce|worcestershire sauce|salsa worcester|pesto|salsa|sauce;120;1.05
capperi|capers|olive|olives|olive nere|olive verdi|black olives|cetriolini|gherkins|pickles;115
basilico|basil|prezzemolo|parsley|rosmarino|rosemary|salvia|sage|timo|thyme|menta|mint|coriandolo|coriander|cilantro|erba cipollina|chives|alloro|bay leaf|bay leaves|aneto|dill|zenzero|ginger|noce moscata|nutmeg|zafferano|saffron|curry|curcuma|turmeric;40
tofu;120
tempeh|seitan;190
latte di cocco|coconut milk|latte di mandorla|almond milk|latte di soia|soy milk|latte di avena|oat milk;40;1
quinoa|couscous|cous cous|farro|spelt|orzo|barley|bulgur|polenta|farina di mais|cornmeal|grano saraceno|buckwheat|miglio|millet;360;0.75
gnocchi;130
pizza margherita;270
"""

# unit -> millilitres
UNIT_ML = {"ml": 1, "cl": 10, "dl": 100, "l": 1000, "tbsp": 15, "tsp": 5, "cup": 240,
           "glass": 200, "can": 400}
# unit -> grams directly
UNIT_G = {"g": 1, "kg": 1000, "oz": 28.35, "lb": 453.6, "pinch": 0.4, "handful": 25,
          "clove": 5, "slice": 25, "pack": 7, "bunch": 30, "sprig": 2, "leaf": 0.5}

# each entry: names -> (Italian suggestions, English suggestions)
SUBS = [
    ("burro|butter", ["olio di oliva o di semi (3/4 della dose)", "margarina", "yogurt greco (nei dolci)"],
     ["olive or vegetable oil (3/4 of the amount)", "margarine", "Greek yogurt (in baking)"]),
    ("panna|panna da cucina|panna fresca|cream|double cream|heavy cream|whipping cream",
     ["latte + un po' di burro (circa 3/4 + 1/4)", "yogurt greco", "mascarpone allungato con latte"],
     ["milk + a little butter (about 3/4 + 1/4)", "Greek yogurt", "mascarpone loosened with milk"]),
    ("latte|milk", ["latte vegetale (soia, avena)", "acqua + un po' di burro", "yogurt allungato con acqua"],
     ["plant milk (soy, oat)", "water + a little butter", "yogurt thinned with water"]),
    ("uova|uovo|egg|eggs",
     ["nei dolci: 1/4 di tazza di yogurt o 1/2 banana schiacciata per uovo", "1 cucchiaio di semi di lino + 3 di acqua"],
     ["in baking: 1/4 cup yogurt or 1/2 mashed banana per egg", "1 tbsp ground flaxseed + 3 tbsp water"]),
    ("lievito per dolci|lievito|baking powder",
     ["bicarbonato + succo di limone (1/4 cucchiaino + 1/2 cucchiaino)", "lievito di birra (per impasti)"],
     ["baking soda + lemon juice (1/4 tsp + 1/2 tsp)", "yeast (for doughs)"]),
    ("bicarbonato|baking soda|bicarbonate of soda", ["lievito per dolci (il triplo)"], ["baking powder (3x the amount)"]),
    ("parmigiano|parmesan|parmigiano reggiano|grana padano|grana",
     ["grana padano", "pecorino romano (piu sapido)", "lievito alimentare in scaglie (vegano)"],
     ["Grana Padano", "pecorino romano (saltier)", "nutritional yeast (vegan)"]),
    ("pecorino|pecorino romano", ["parmigiano reggiano", "grana padano"], ["Parmesan", "Grana Padano"]),
    ("pancetta|bacon", ["guanciale", "speck a listarelle", "prosciutto cotto a dadini"],
     ["guanciale", "diced speck", "diced cooked ham"]),
    ("guanciale", ["pancetta", "bacon"], ["pancetta", "streaky bacon"]),
    ("buttermilk", ["latte + 1 cucchiaio di succo di limone (riposo 5 min)", "yogurt allungato con latte"],
     ["milk + 1 tbsp lemon juice (rest 5 min)", "yogurt thinned with milk"]),
    ("yogurt|yoghurt|greek yogurt|yogurt greco", ["panna acida", "ricotta", "mascarpone"], ["sour cream", "ricotta", "mascarpone"]),
    ("panna acida|sour cream", ["yogurt greco", "panna + succo di limone"], ["Greek yogurt", "cream + lemon juice"]),
    ("mascarpone", ["ricotta + panna", "formaggio spalmabile"], ["ricotta + cream", "cream cheese"]),
    ("ricotta", ["mascarpone", "formaggio fresco spalmabile", "fiocchi di latte"], ["mascarpone", "cream cheese", "cottage cheese"]),
    ("mozzarella", ["scamorza", "provola", "fiordilatte"], ["scamorza", "provola", "fior di latte"]),
    ("zucchero|sugar|zucchero semolato", ["zucchero di canna", "miele (3/4 della dose, meno liquidi)", "sciroppo d'acero"],
     ["brown sugar", "honey (3/4 of the amount, less liquid)", "maple syrup"]),
    ("zucchero di canna|brown sugar", ["zucchero bianco + un cucchiaino di melassa/miele"], ["white sugar + a spoon of molasses/honey"]),
    ("zucchero a velo|icing sugar", ["zucchero semolato frullato finissimo"], ["caster sugar blitzed to powder"]),
    ("miele|honey", ["sciroppo d'acero", "zucchero + un po' d'acqua"], ["maple syrup", "sugar + a little water"]),
    ("farina 00|farina|flour|plain flour|all-purpose flour",
     ["farina 0", "farina integrale (80%)", "mix farina di riso e fecola"], ["bread flour", "wholemeal (80%)", "rice flour + starch mix"]),
    ("farina manitoba|bread flour", ["farina 0 + un po' di glutine", "farina 00 (lievitazione piu bassa)"], ["plain flour (less rise)"]),
    ("fecola|amido di mais|cornflour|cornstarch", ["maizena", "farina (il doppio)", "amido di riso"], ["cornflour", "plain flour (2x)", "rice starch"]),
    ("pangrattato|breadcrumbs", ["pane secco frullato", "fiocchi di mais frantumati", "farina di mandorle"],
     ["blitzed stale bread", "crushed cornflakes", "ground almonds"]),
    ("vino bianco|white wine", ["brodo + un goccio di aceto o limone", "vermouth secco"], ["stock + a splash of vinegar or lemon", "dry vermouth"]),
    ("vino rosso|red wine", ["brodo + aceto balsamico", "succo di melograno"], ["stock + balsamic vinegar", "pomegranate juice"]),
    ("brodo|stock|broth", ["dado + acqua", "acqua + sale e un pezzo di verdura"], ["stock cube + water", "water + salt and a vegetable"]),
    ("aglio|garlic", ["aglio in polvere (1/4 cucchiaino per spicchio)", "scalogno"], ["garlic powder (1/4 tsp per clove)", "shallot"]),
    ("cipolla|onion", ["scalogno", "porro", "cipolla in polvere"], ["shallot", "leek", "onion powder"]),
    ("scalogno|shallot", ["cipolla bianca", "porro"], ["white onion", "leek"]),
    ("prezzemolo|parsley", ["coriandolo", "basilico", "erba cipollina"], ["coriander", "basil", "chives"]),
    ("basilico|basil", ["origano", "prezzemolo", "rucola (nel pesto)"], ["oregano", "parsley", "rocket (for pesto)"]),
    ("rosmarino|rosemary", ["timo", "salvia", "origano"], ["thyme", "sage", "oregano"]),
    ("timo|thyme", ["maggiorana", "origano"], ["marjoram", "oregano"]),
    ("limone|lemon|succo di limone|lemon juice", ["lime", "aceto bianco (meta dose)", "arancia"], ["lime", "white vinegar (half amount)", "orange"]),
    ("aceto|vinegar|white wine vinegar", ["succo di limone", "aceto di mele"], ["lemon juice", "cider vinegar"]),
    ("aceto balsamico|balsamic vinegar", ["aceto di vino rosso + un pizzico di zucchero"], ["red wine vinegar + a pinch of sugar"]),
    ("pomodori pelati|pelati|chopped tomatoes|tinned tomatoes|canned tomatoes",
     ["passata di pomodoro", "pomodori freschi maturi", "polpa di pomodoro"], ["passata", "ripe fresh tomatoes", "tomato pulp"]),
    ("passata|passata di pomodoro|salsa di pomodoro", ["pelati frullati", "pomodori freschi"], ["blitzed tinned tomatoes", "fresh tomatoes"]),
    ("concentrato di pomodoro|tomato paste", ["passata ridotta a fuoco lento", "ketchup (poco)"], ["reduced passata", "ketchup (a little)"]),
    ("cipollotto|spring onion|spring onions", ["erba cipollina", "porro", "cipolla"], ["chives", "leek", "onion"]),
    ("riso|rice|riso arborio", ["orzo perlato", "farro", "quinoa"], ["pearl barley", "spelt", "quinoa"]),
    ("spaghetti", ["linguine", "bucatini", "tagliolini"], ["linguine", "bucatini", "tagliolini"]),
    ("penne", ["rigatoni", "fusilli", "maccheroni"], ["rigatoni", "fusilli", "macaroni"]),
    ("pasta", ["altro formato di pasta secca", "pasta integrale"], ["any dried pasta shape", "wholewheat pasta"]),
    ("cacao|cacao amaro|cocoa", ["cioccolato fondente fuso (il doppio)", "carruba in polvere"], ["melted dark chocolate (2x)", "carob powder"]),
    ("cioccolato fondente|dark chocolate", ["gocce di cioccolato", "cacao + zucchero + burro"], ["chocolate chips", "cocoa + sugar + butter"]),
    ("mandorle|almonds", ["nocciole", "noci", "pistacchi"], ["hazelnuts", "walnuts", "pistachios"]),
    ("pinoli|pine nuts", ["mandorle", "anacardi", "girasole"], ["almonds", "cashews", "sunflower seeds"]),
    ("noci|walnuts", ["pecan", "nocciole", "mandorle"], ["pecans", "hazelnuts", "almonds"]),
    ("panna da montare|whipping cream", ["panna vegetale", "mascarpone + latte"], ["plant-based cream", "mascarpone + milk"]),
    ("pasta sfoglia|puff pastry", ["pasta brisee", "pasta fillo"], ["shortcrust pastry", "filo pastry"]),
    ("olio di semi|vegetable oil|sunflower oil", ["olio di oliva", "olio di arachidi"], ["olive oil", "peanut oil"]),
    ("olio d'oliva|olio extravergine|olive oil|olio", ["olio di semi", "burro (nelle cotture)"], ["vegetable oil", "butter (for cooking)"]),
    ("salsa di soia|soy sauce", ["tamari", "salsa worcester (poca)", "sale + brodo"], ["tamari", "Worcestershire sauce (less)", "salt + stock"]),
    ("senape|mustard", ["wasabi (poco)", "salsa tartara"], ["horseradish (a little)", "tartare sauce"]),
    ("zenzero|ginger", ["zenzero in polvere (1/4 della dose)", "cannella"], ["ground ginger (1/4 of the amount)", "cinnamon"]),
    ("zafferano|saffron", ["curcuma (un pizzico)", "paprika dolce"], ["turmeric (a pinch)", "sweet paprika"]),
    ("noce moscata|nutmeg", ["cannella", "macis"], ["cinnamon", "mace"]),
    ("peperoncino|chilli|chili", ["pepe nero", "paprika piccante", "tabasco"], ["black pepper", "hot paprika", "Tabasco"]),
    ("pangrattato", ["farina di mais", "corn flakes"], ["cornmeal", "cornflakes"]),
    ("vaniglia|vanilla", ["scorza di limone", "aroma alla vaniglia", "vanillina"], ["lemon zest", "vanilla essence", "vanillin"]),
    ("lievito di birra|yeast", ["lievito madre (3x)", "lievito secco (1/3 della dose)"], ["sourdough starter (3x)", "dried yeast (1/3 of the amount)"]),
    ("tonno|tuna", ["salmone in scatola", "sgombro"], ["canned salmon", "mackerel"]),
    ("pollo|chicken|petto di pollo|chicken breast", ["tacchino", "maiale (lonza)", "tofu (vegetariano)"], ["turkey", "pork loin", "tofu (vegetarian)"]),
    ("manzo|beef|macinato|minced beef", ["macinato misto", "tacchino macinato", "lenticchie"], ["mixed mince", "turkey mince", "lentils"]),
    ("salsiccia|sausage", ["macinato di maiale + spezie", "pancetta"], ["pork mince + spices", "bacon"]),
    ("prosciutto cotto|ham", ["speck", "tacchino arrosto"], ["speck", "roast turkey"]),
    ("gorgonzola|blue cheese", ["taleggio", "roquefort", "formaggio erborinato"], ["taleggio", "Roquefort", "any blue cheese"]),
    ("feta", ["ricotta salata", "caprino", "halloumi"], ["ricotta salata", "goat's cheese", "halloumi"]),
    ("gnocchi", ["patate bollite + farina"], ["boiled potatoes + flour"]),
    ("avena|oats", ["fiocchi di farro", "muesli"], ["spelt flakes", "muesli"]),
    ("latte condensato|condensed milk", ["latte + zucchero ridotti a fuoco lento"], ["milk + sugar reduced on the hob"]),
    ("sedano|celery", ["finocchio", "prezzemolo (gambi)"], ["fennel", "parsley stalks"]),
]
