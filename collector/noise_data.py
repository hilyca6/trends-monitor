"""Static word lists for rule-based noise detection (no LLM required).

These let the cheap, offline rule pass catch the most common "knowledge" noise
(sports fixtures/leagues/clubs, brand & media names, person names) so the LLM
only has to handle the residue.

Design notes:
  * Kept as plain sets/frozensets for fast membership tests.
  * Deliberately high-precision: only well-known leagues/clubs/brands and common
    English given names + surnames. Anything uncertain is left to the LLM pass.
  * The AI / finance brands are intentionally NOT listed as noise, because those
    are the target niches for this project. Edit these lists freely.
"""

# League names that appear as a single token (matched token-exact).
LEAGUES = frozenset(
    {
        "nba", "nfl", "mlb", "nhl", "wnba", "mls", "ncaa", "nrl", "ipl", "bbl",
        "nbl", "ufc", "wwe", "afl", "cfl", "kbo", "lck", "lpl", "lcs", "epl",
        "bundesliga", "eredivisie", "primeira", "ligue", "superliga", "concacaf",
        "afcon", "copa", "uefa", "atp", "wta", "pga", "motogp",
    }
)

# Multi-word league / competition names (matched as substrings).
LEAGUE_PHRASES = frozenset(
    {
        "premier league", "la liga", "serie a", "champions league",
        "europa league", "conference league", "fa cup", "copa del rey",
        "copa america", "world cup", "super bowl", "world series",
        "stanley cup", "nba finals", "formula 1", "grand prix", "saudi pro league",
        "super lig", "primeira liga", "copa libertadores", "premier division",
    }
)

# Clubs that appear as a single token.
CLUBS = frozenset(
    {
        "tottenham", "arsenal", "chelsea", "liverpool", "everton", "barcelona",
        "madrid", "juventus", "napoli", "ajax", "benfica", "porto", "galatasaray",
        "fenerbahce", "flamengo", "palmeiras", "corinthians", "sevilla",
        "valencia", "lazio", "dortmund",
    }
)

# Multi-word club names (matched as substrings).
CLUB_PHRASES = frozenset(
    {
        "al nassr", "al hilal", "al ahli", "inter miami", "manchester united",
        "manchester city", "real madrid", "atletico madrid", "bayern munich",
        "borussia dortmund", "paris saint", "inter milan", "ac milan",
        "newcastle united", "west ham", "crystal palace", "aston villa",
        "leicester city", "tottenham hotspur", "la galaxy", "psg",
    }
)

# Sports-news words that strongly indicate a fixture/result (matched token-exact).
SPORTS_EVENT_WORDS = frozenset(
    {
        "fixtures", "highlights", "standings", "scorecard", "innings", "wicket",
        "kickoff", "matchday", "playoff", "playoffs", "semifinal", "semifinals",
        "quarterfinal", "quarterfinals", "h2h", "bracket",
    }
)

# Brand / media / ticketing / betting names unlikely to be SEO targets here.
BRAND_WORDS = frozenset(
    {
        "ticketmaster", "stubhub", "seatgeek", "fanduel", "draftkings", "bet365",
        "betfair", "pokerstars", "hbo", "hulu", "peacock", "paramount", "sirius",
        "cnn", "ndtv", "msn", "bbc", "nbc", "cbs", "disney", "espn", "xfinity",
        "comcast", "verizon", "vodafone",
    }
)

BRAND_PHRASES = frozenset(
    {
        "hbo max", "amazon prime", "prime video", "disney plus", "apple tv",
        "paramount plus", "peacock tv", "discovery plus",
    }
)

# Common English given names (lowercased).
FIRST_NAMES = frozenset(
    {
        "aaron", "adam", "adrian", "alan", "albert", "alex", "alexander",
        "alexis", "alice", "amanda", "amber", "amy", "andrea", "andrew",
        "angela", "ann", "anna", "anthony", "arthur", "ashley", "austin",
        "barbara", "benjamin", "betty", "bill", "billy", "bob", "bobby",
        "brandon", "brenda", "brian", "bruce", "bryan", "caleb", "cameron",
        "carl", "carlos", "carmen", "carol", "caroline", "carrie", "catherine",
        "chad", "charles", "charlie", "charlotte", "chris", "christina",
        "christine", "christopher", "cindy", "claire", "clara", "craig",
        "crystal", "curtis", "cynthia", "daniel", "danielle", "danny", "david",
        "dawn", "dean", "deborah", "debra", "denise", "dennis", "derek",
        "diana", "diane", "donald", "donna", "doris", "dorothy", "doug",
        "douglas", "dylan", "edward", "elijah", "elizabeth", "ella", "emily",
        "emma", "eric", "erica", "erin", "ethan", "eugene", "evan", "evelyn",
        "frank", "fred", "gabriel", "gary", "george", "gerald", "gina",
        "gloria", "grace", "gregory", "hannah", "harold", "harry", "heather",
        "helen", "henry", "holly", "howard", "ian", "irene", "isaac", "jack",
        "jackie", "jacob", "jaime", "james", "jamie", "jane", "janet",
        "janice", "jared", "jason", "jay", "jeff", "jeffrey", "jenna",
        "jennifer", "jeremy", "jerry", "jesse", "jessica", "jill", "jim",
        "jimmy", "joan", "joe", "joel", "joey", "john", "johnny", "jonathan",
        "jordan", "jose", "joseph", "joshua", "joyce", "juan", "judith",
        "judy", "julia", "julie", "justin", "karen", "katherine", "kathleen",
        "kathryn", "kathy", "katie", "keith", "kelly", "ken", "kenneth",
        "kevin", "kim", "kimberly", "kristen", "kyle", "larry", "laura",
        "lauren", "lawrence", "leah", "leo", "leonard", "leslie", "liam",
        "lillian", "lily", "linda", "lisa", "logan", "lois", "lori", "louis",
        "lucas", "lucy", "luke", "lydia", "madison", "marcus", "margaret",
        "maria", "marie", "marilyn", "mark", "martha", "martin", "mary",
        "mason", "matthew", "matt", "maureen", "max", "megan", "melanie",
        "melissa", "mia", "michael", "michele", "michelle", "mike", "mildred",
        "molly", "monica", "morgan", "nancy", "natalie", "nathan", "nicholas",
        "nick", "nicole", "noah", "norman", "olivia", "oscar", "pamela",
        "patricia", "patrick", "paul", "paula", "peggy", "peter", "philip",
        "phillip", "rachel", "ralph", "randall", "randy", "raymond", "rebecca",
        "regina", "richard", "rick", "ricky", "rita", "robert", "roberto",
        "robin", "rodney", "roger", "ronald", "rosa", "rose", "ross", "roy",
        "russell", "ruth", "ryan", "samantha", "samuel", "sandra", "sara",
        "sarah", "scott", "sean", "sharon", "shawn", "sheila", "shirley",
        "simon", "sophia", "stacey", "stacy", "stanley", "stephanie",
        "stephen", "steve", "steven", "sue", "susan", "tammy", "tanya",
        "taylor", "teresa", "terry", "theresa", "thomas", "tiffany", "tim",
        "timothy", "tina", "todd", "tom", "tommy", "tony", "tracy", "travis",
        "tyler", "valerie", "vanessa", "victor", "victoria", "vincent",
        "virginia", "walter", "wanda", "warren", "wayne", "wendy", "william",
        "willie", "zachary",
    }
)

# Common English surnames (lowercased).
SURNAMES = frozenset(
    {
        "adams", "allen", "anderson", "andrews", "armstrong", "arnold",
        "bailey", "baker", "barnes", "bell", "bennett", "berry", "bishop",
        "black", "bowman", "boyd", "bradley", "brooks", "brown", "bryant",
        "burke", "burns", "butler", "campbell", "carlson", "carroll", "carter",
        "castillo", "chapman", "clark", "clarke", "cole", "coleman", "collins",
        "cook", "cooper", "cox", "crawford", "cunningham", "curtis", "davis",
        "dawson", "dixon", "duncan", "dunlap", "ellis", "evans", "ferguson",
        "fields", "fisher", "fleming", "fletcher", "ford", "foster", "fox",
        "franklin", "freeman", "frost", "fuller", "garcia", "gardner",
        "garrett", "gibson", "gilbert", "gomez", "gonzalez", "gordon",
        "graham", "grant", "gray", "green", "greene", "griffin", "hall",
        "hamilton", "hansen", "harding", "harris", "harrison", "hart",
        "harvey", "hawkins", "hayes", "henderson", "hernandez", "hicks",
        "hill", "holland", "holmes", "holt", "hopkins", "howard", "hudson",
        "hughes", "hunt", "hunter", "jackson", "jenkins", "johnson",
        "johnston", "jones", "keller", "kennedy", "kent", "kerr", "king",
        "knight", "lambert", "lane", "larson", "lawson", "lee", "lewis",
        "long", "lopez", "lowe", "lynch", "marsh", "marshall", "martin",
        "matthews", "maxwell", "mccarthy", "mcdonald", "mckinney", "miles",
        "miller", "mills", "mitchell", "moore", "morgan", "morris",
        "morrison", "murphy", "murray", "nelson", "newman", "nichols",
        "nixon", "oliver", "olsen", "olson", "owens", "palmer", "parker",
        "parsons", "patterson", "payne", "pearson", "perkins", "perry",
        "peters", "peterson", "phillips", "pierce", "porter", "powell",
        "price", "prince", "quinn", "ramirez", "ramos", "reid", "reyes",
        "reynolds", "rhodes", "rice", "richardson", "riley", "rivera",
        "roberts", "robertson", "robinson", "rodriguez", "rogers", "rose",
        "ross", "russell", "ryan", "sanders", "sawyer", "schmidt", "scott",
        "shaw", "simmons", "simpson", "sinclair", "smith", "snyder",
        "spencer", "stanley", "stevens", "stewart", "stone", "sullivan",
        "sutton", "taylor", "thomas", "thompson", "tucker", "turner",
        "walker", "wallace", "walsh", "ward", "warren", "watson", "watts",
        "weaver", "webb", "wells", "west", "wheeler", "white", "williams",
        "willis", "wilson", "wood", "woods", "wright", "young",
    }
)
