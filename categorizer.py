"""Transaction categorization: user rules first, then keyword defaults."""

from models import CategoryRule

# Expanded default keyword → category map
DEFAULT_RULES = {
    "Income": [
        "DIR DEP", "PAYROLL", "DIRECT DEPOSIT",
    ],
    "Dining": [
        "RESTAURANT", "DOORDASH", "GRUBHUB", "UBER EATS", "UBEREATS",
        "SWEETGREEN", "CHICK-FIL", "STARBUCKS", "DUTCH BROS", "DUNKIN",
        "CHICKEN", "NOODLE", "BBQ", "BAKERY", "CAFE", "KITCHEN",
        "SALADS", "SHOKUDO", "SONGQIREN", "SUSHI", "PIZZA", "TACO",
        "BURGER", "PANDA EXPRESS", "CHIPOTLE", "PANERA", "SUBWAY",
        "MCDONALD", "WENDY", "POPEYES", "WHATABURGER", "IN-N-OUT",
        "FIVE GUYS", "SHAKE SHACK", "WINGSTOP", "BUFFALO WILD",
        "IHOP", "DENNY", "WAFFLE", "CRACKER BARREL",
        "CENTREKITCHEN", "SNAPPY SALAD", "KOREAN BBQ", "SAEMAEUL",
        "KUNG FU", "MIKE'S CHICKEN", "TOKYO SHOKUDO", "85C BAKERY",
        "KING'S NOODLE", "A CHICKEN",
        "DD *DOORDASH", "TST*", "SQ *",
    ],
    "Groceries": [
        "WAL-MART", "WALMART", "WM SUPERCENTER", "WHOLEFDS", "WHOLE FOODS",
        "GROCERY", "COSTCO", "TRADER JOE", "HEB", "KROGER", "TARGET",
        "SAFEWAY", "ALBERTSON", "PUBLIX", "ALDI", "SPROUTS", "FOOD LION",
        "WEGMANS", "MARKET", "FRESH MARKET", "SAM'S CLUB",
        "H-E-B", "WINCO", "FOOD", "PIGGLY",
    ],
    "Travel": [
        "AMERICAN AIR", "UNITED AIR", "DELTA AIR", "SOUTHWEST", "JETBLUE",
        "SPIRIT AIR", "FRONTIER", "ALASKA AIR",
        "LYFT", "UBER TRIP", "UBER *TRIP",
        "HOTEL", "HILTON", "MARRIOTT", "HYATT", "IHG", "AIRBNB", "VRBO",
        "AIRLINE", "EXPEDIA.COM", "BOOKING.COM",
        "HERTZ", "ENTERPRISE", "NATIONAL CAR", "AVIS", "BUDGET RENT",
        "TSA", "AIRPORT",
    ],
    "Transfer": [
        "ZELLE", "VENMO", "CASHAPP", "PAYPAL",
        "TRANSFER", "AUTOPAY", "AUTOMATIC PAYMENT",
        "ONLINE REALTIME TRANSFER", "ACH PMT",
        "CREDIT CRD AUTOPAY", "CITI AUTOPAY",
    ],
    "Housing": [
        "MORTGAGE", "CROSSCOUNTRY", "RENT", "LEASE",
        "HOME DEPOT", "LOWE'S", "LOWES",
        "PROPERTY TAX", "HOA",
    ],
    "Shopping": [
        "AMAZON", "AMZN", "APPLE.COM", "BEST BUY", "BESTBUY",
        "NORDSTROM", "MACY", "ROSS", "TJ MAXX", "MARSHALLS",
        "NIKE", "ADIDAS", "GAP", "OLD NAVY", "H&M", "ZARA",
        "IKEA", "WAYFAIR", "ETSY", "EBAY",
    ],
    "Subscription": [
        "NETFLIX", "SPOTIFY", "YOUTUBE", "HULU", "DISNEY+", "HBO",
        "APPLE MUSIC", "AMAZON PRIME", "AUDIBLE",
        "7SAGE", "FIFA", "FIFAUS", "OPENAI", "CHATGPT",
        "ADOBE", "MICROSOFT 365", "GOOGLE STORAGE",
        "GYM", "PLANET FITNESS", "EQUINOX", "YMCA",
    ],
    "Gas": [
        "GAS STATION", "SHELL", "EXXON", "CHEVRON", "BP ",
        "CIRCLE K", "7-ELEVEN", "WAWA", "MURPHY", "RACETRAC",
        "QUIKTRIP", "SPEEDWAY", "VALERO", "SUNOCO", "CITGO",
    ],
    "Utilities": [
        "ELECTRIC", "WATER BILL", "GAS BILL", "INTERNET",
        "COMCAST", "XFINITY", "AT&T", "T-MOBILE", "VERIZON", "SPRINT",
        "SPECTRUM", "COX COMM", "GOOGLE FI",
    ],
    "Healthcare": [
        "PHARMACY", "CVS", "WALGREENS", "RITE AID",
        "DOCTOR", "HOSPITAL", "MEDICAL", "DENTAL", "OPTOM",
        "LABCORP", "QUEST DIAG", "URGENT CARE",
        "INSURANCE", "COPAY",
    ],
    "Entertainment": [
        "MOVIE", "AMC ", "REGAL", "CINEMARK",
        "TICKETMASTER", "STUBHUB", "LIVE NATION",
        "STEAM", "PLAYSTATION", "XBOX", "NINTENDO",
        "GOLF", "BOWLING", "TOPGOLF",
    ],
    "Education": [
        "TUITION", "UNIVERSITY", "COLLEGE", "SCHOOL",
        "COURSERA", "UDEMY", "SKILLSHARE", "MASTERCLASS",
        "TEXTBOOK", "CHEGG",
    ],
    "Personal Care": [
        "SALON", "BARBER", "HAIR", "SPA", "MASSAGE", "NAIL",
        "SEPHORA", "ULTA", "BATH & BODY",
    ],
    "Pet": [
        "PETCO", "PETSMART", "PET SUPPLIES", "CHEWY", "VET", "VETERINAR",
        "BANFIELD", "ANIMAL HOSPITAL", "PET FOOD", "BARK BOX", "BARKBOX",
    ],
}


def categorize(description, db_session=None):
    """Categorize a transaction description. Checks user rules first, then defaults."""
    desc_upper = description.upper() if description else ""

    # 1. Check user-defined rules (from DB)
    if db_session:
        rules = db_session.query(CategoryRule).all()
        for rule in rules:
            if rule.keyword.upper() in desc_upper:
                return rule.category

    # 2. Fall back to default keywords
    for category, keywords in DEFAULT_RULES.items():
        for keyword in keywords:
            if keyword in desc_upper:
                return category

    return "Other"


def get_all_categories():
    """Return list of all known category names."""
    return sorted(DEFAULT_RULES.keys()) + ["Other"]
