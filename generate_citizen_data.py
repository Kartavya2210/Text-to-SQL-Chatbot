#!/usr/bin/env python3
"""
Generate 10 lakh (1,000,000) realistic Indian citizen records for SQLite.
Focused on Kerala citizens (~90%) with realistic distributions.
Run: python generate_citizen_data.py
"""
import sqlite3, random, string, os, sys, time
from datetime import date, timedelta

DB_PATH = "citizen_data.db"
BATCH    = 5_000
TOTAL    = 1_000_000

# ── Data pools ──────────────────────────────────────────────────────────────
MALE_FIRST = ["Arun","Rahul","Vijay","Suresh","Rajesh","Manoj","Anoop","Sanjay",
              "Deepak","Rajan","Arjun","Krishnan","Sivadas","Babu","Mohan","Biju",
              "Shyam","Sasi","Bineesh","Vineesh","Ajith","Pradeep","Ramesh","Unni",
              "Santhosh","Dileep","Jibin","Midhun","Sajeev","Noufal","Abdul","Hameed",
              "Jithin","Vishnu","Nikhil","Akshay","Rohit","Amith","Sreejith","Gireesh",
              "Rajeev","Pramod","Bipin","Shinoy","Tijo","Albin","Jijo","Sijo","Reji",
              "Hari","Balan","Ravi","Sunil","Anil","Manu","Lal","Sibu","Shibu","Tomy"]

FEMALE_FIRST = ["Priya","Anjali","Deepa","Rekha","Latha","Suja","Asha","Nisha",
                "Kavya","Meera","Sindhu","Bindhu","Smitha","Sheeba","Reena","Soumya",
                "Neethu","Manju","Divya","Sreeja","Fathima","Nasrin","Anu","Beena",
                "Parvathy","Geetha","Saritha","Sunitha","Ranjitha","Usha","Seema",
                "Arya","Amitha","Chithra","Ambika","Subha","Dhanya","Vidya","Hema",
                "Reshma","Sini","Mini","Liji","Shiny","Lincy","Jincy","Tessy","Rosy",
                "Leena","Sheena","Deena","Seena","Veena","Beena","Meena","Sheenu"]

LAST_NAMES = ["Nair","Menon","Pillai","Varma","Kurup","Krishnan","Rajan","Thomas",
              "Joseph","George","Mathew","Paul","Peter","John","Xavier","Philip",
              "Vijayan","Kumar","Das","Sharma","Khan","Ansari","Nadar","Thevar",
              "Iyer","Iyengar","Babu","Raj","Reddy","Naidu","Rao","Murthy",
              "Mohan","Suresh","Ramesh","Bose","Chatterjee","Patel","Shah","Verma",
              "Thampi","Achary","Namboothiri","Valsan","Ramesan","Gopalan","Chandran"]

MIDDLE = ["Rajan","Mohan","Kumar","Babu","Das","Krishnan","Pillai","Lal","Prasad",
          "Raj","Dev","Hari","Balan","Vijayan","Suresh","Ramesh","","","","",""]

KERALA_DISTRICTS = [
    ("Thiruvananthapuram","32001","TVP"),("Kollam","32002","KLM"),
    ("Pathanamthitta","32003","PAT"),("Alappuzha","32004","ALP"),
    ("Kottayam","32005","KTM"),("Idukki","32006","IDK"),
    ("Ernakulam","32007","EKM"),("Thrissur","32008","TSR"),
    ("Palakkad","32009","PKD"),("Malappuram","32010","MLP"),
    ("Kozhikode","32011","KZD"),("Wayanad","32012","WYD"),
    ("Kannur","32013","KNR"),("Kasaragod","32014","KSD"),
]

OTHER_STATES = {
    "Tamil Nadu":     [("Chennai","33001"),("Coimbatore","33002"),("Madurai","33003"),("Salem","33004")],
    "Karnataka":      [("Bengaluru","29001"),("Mysuru","29002"),("Mangaluru","29003")],
    "Andhra Pradesh": [("Visakhapatnam","28001"),("Vijayawada","28002"),("Guntur","28003")],
    "Maharashtra":    [("Mumbai","27001"),("Pune","27002"),("Nagpur","27003")],
    "Delhi":          [("New Delhi","07001"),("North Delhi","07002")],
    "Gujarat":        [("Ahmedabad","24001"),("Surat","24002"),("Vadodara","24003")],
    "Telangana":      [("Hyderabad","36001"),("Warangal","36002")],
    "Uttar Pradesh":  [("Lucknow","09001"),("Agra","09002"),("Kanpur","09003")],
}
STATE_LGD = {"Kerala":"32","Tamil Nadu":"33","Karnataka":"29","Andhra Pradesh":"28",
             "Maharashtra":"27","Delhi":"07","Gujarat":"24","Telangana":"36","Uttar Pradesh":"09"}

VILLAGES = {
    "Thiruvananthapuram":["Kovalam","Varkala","Nedumangad","Neyyattinkara","Attingal","Balaramapuram"],
    "Kollam":            ["Karunagappally","Kottarakkara","Punalur","Paravur","Chavara","Kulathupuzha"],
    "Ernakulam":         ["Aluva","Perumbavoor","Muvattupuzha","Angamaly","Kothamangalam","Thrippunithura"],
    "Thrissur":          ["Guruvayur","Chalakudy","Irinjalakuda","Kodungallur","Kunnamkulam","Chavakkad"],
    "Kozhikode":         ["Vatakara","Koyilandy","Ramanattukara","Koduvally","Perambra","Thamarassery"],
    "Malappuram":        ["Tirur","Manjeri","Ponnani","Perinthalmanna","Kondotty","Tirurrangadi"],
    "Palakkad":          ["Ottapalam","Shoranur","Mannarkkad","Alathur","Chittur","Pattambi"],
    "Kannur":            ["Thalassery","Iritty","Payyanur","Koothuparamba","Mattannur","Payyannur"],
    "default":           ["Ward 1","Ward 2","Ward 3","Ward 4","Ward 5","Ward 6","Ward 7"],
}

KL_PINCODES = list(range(670001,670025))+list(range(671001,671020))+list(range(673001,673020))+\
              list(range(676001,676015))+list(range(678001,678015))+list(range(680001,680020))+\
              list(range(682001,682020))+list(range(686001,686015))+list(range(688001,688015))+\
              list(range(691001,691015))+list(range(695001,695025))

RELIGIONS   = ["Hindu","Muslim","Christian","Others"]
REL_W       = [0.55, 0.27, 0.18, 0.00]
CATEGORIES  = ["General","OBC","SC","ST","EWS"]
CAT_W       = [0.40, 0.30, 0.15, 0.05, 0.10]
CASTES      = {"General":["Nair","Brahmin","Menon","Pillai","Varma","Kurup"],
               "OBC":    ["Ezhava","Thiyya","Mappila","Nadar","Vellalar","Chettiar"],
               "SC":     ["Pulaya","Paraya","Vedar","Cheruman","Sambavar"],
               "ST":     ["Adivasi","Paniya","Kurumba","Irula","Malayan"],
               "EWS":    ["Nair","Christian","Mappila","Brahmin"]}
CAT_ID      = {"General":"CAT01","OBC":"CAT02","SC":"CAT03","ST":"CAT04","EWS":"CAT05"}

OCCUPATIONS = ["Farmer","Government Employee","Private Employee","Business","Daily Wage Worker",
               "Teacher","Doctor","Engineer","Driver","Homemaker","Student","Retired",
               "Fisherman","Construction Worker","IT Professional","Shop Owner","Nurse",
               "Electrician","Plumber","Auto Driver","Migrant Worker","Others"]
EMP_STATUS  = ["Employed","Unemployed","Self-Employed","Student","Retired","Homemaker"]
EMP_TYPE    = ["Government","Private","Semi-Government","Contract","Daily Wage","Self","Others",""]
MARITAL     = ["Single","Married","Widowed","Divorced","Separated"]
MAR_W       = [0.25, 0.60, 0.08, 0.04, 0.03]

BANKS = [("State Bank of India","SBIN"),("Kerala Gramin Bank","KLGB"),("Canara Bank","CNRB"),
         ("Union Bank of India","UBIN"),("Bank of Baroda","BARB"),("HDFC Bank","HDFC"),
         ("ICICI Bank","ICIC"),("Federal Bank","FDRL"),("South Indian Bank","SIBL"),
         ("Catholic Syrian Bank","CSBK"),("Kerala Bank","KLBK"),("Dhanlaxmi Bank","DLXB"),
         ("Punjab National Bank","PUNB"),("Axis Bank","UTIB")]

VEH_TYPES = ["Two-Wheeler","Four-Wheeler","Auto-Rickshaw","Truck","None","None"]
FUEL_TYPES = ["Petrol","Diesel","CNG","Electric",""]
RAT_TYPES  = ["APL","BPL","AAY","PHH","NPHH",""]
RAT_W      = [0.20,0.15,0.05,0.30,0.20,0.10]
DIS_TYPES  = ["Locomotor","Visual","Hearing","Speech","Intellectual","Mental","Multiple"]
HI_TYPES   = ["Karunya","PMJAY","RSBY","ESI","Private","None"]
LPG_TYPES  = ["Indane","HP Gas","Bharat","Others",""]
KSEB_TIERS = ["LT-1A","LT-1B","LT-2","LT-3","LT-4",""]

# ── Helpers ──────────────────────────────────────────────────────────────────
def rd(n): return ''.join(random.choices(string.digits, k=n))
def ru(n): return ''.join(random.choices(string.ascii_uppercase, k=n))
def ra(n): return ''.join(random.choices(string.ascii_uppercase+string.digits, k=n))
def maybe(val, prob=0.5): return val if random.random() < prob else None

def dob_and_age(min_age=0, max_age=95):
    today = date.today()
    d = today - timedelta(days=random.randint(min_age*365, max_age*365))
    age = today.year - d.year - ((today.month, today.day) < (d.month, d.day))
    return d.isoformat(), max(0, age)

# ── Record generator ─────────────────────────────────────────────────────────
def gen():
    gender = random.choices(["Male","Female","Transgender"],[0.51,0.48,0.01])[0]
    fname  = random.choice(MALE_FIRST if gender=="Male" else FEMALE_FIRST)
    mname  = random.choice(MIDDLE)
    lname  = random.choice(LAST_NAMES)
    name   = (fname+" "+mname+" "+lname).replace("  "," ").strip()

    dob, age = dob_and_age(0, 95)

    # Geography — 90% Kerala
    if random.random() < 0.90:
        state, state_lgd = "Kerala", "32"
        dist, dist_lgd, _ = random.choice(KERALA_DISTRICTS)
        pincode = str(random.choice(KL_PINCODES))
    else:
        state = random.choice(list(OTHER_STATES))
        state_lgd = STATE_LGD.get(state,"00")
        dist, dist_lgd = random.choice(OTHER_STATES[state])
        pincode = rd(6)

    village = random.choice(VILLAGES.get(dist, VILLAGES["default"]))
    taluk   = dist + " Taluk " + str(random.randint(1,5))

    addr1 = f"{random.randint(1,999)}/{ra(3)}"
    addr2 = random.choice(["Near Temple","Near Mosque","Near School","Near Market","Main Road","Cross Road"])
    addr3 = f"{village}, {dist}"

    religion = random.choices(RELIGIONS, weights=REL_W)[0]
    category = random.choices(CATEGORIES, weights=CAT_W)[0]
    caste    = random.choice(CASTES.get(category,["Others"]))

    marital = random.choices(MARITAL, weights=MAR_W)[0]
    spouse  = None
    if marital == "Married":
        sfname = random.choice(FEMALE_FIRST if gender=="Male" else MALE_FIRST)
        spouse = sfname + " " + random.choice(LAST_NAMES)

    father_n = random.choice(MALE_FIRST)   + " " + random.choice(LAST_NAMES)
    mother_n = random.choice(FEMALE_FIRST) + " " + random.choice(LAST_NAMES)
    fam_cnt  = random.randint(1,8)

    emp_st  = random.choices(EMP_STATUS,[0.45,0.15,0.15,0.10,0.10,0.05])[0]
    emp_typ = random.choice(EMP_TYPE) if emp_st=="Employed" else ""
    occ     = random.choice(OCCUPATIONS)
    is_govt = emp_typ == "Government"
    income  = round(random.uniform(50000,1500000),2) if emp_st not in ["Student","Homemaker","Unemployed"] else 0.0
    inc_tax = income > 500000

    # Education (age-gated)
    matric  = age>=14 and random.random()<0.70
    twlft   = matric and age>=16 and random.random()<0.60
    grad    = twlft  and age>=21 and random.random()<0.45
    pg      = grad   and age>=23 and random.random()<0.30

    # Disability
    dis   = random.random()<0.07
    dtype = random.choice(DIS_TYPES) if dis else None
    dpct  = round(random.uniform(40,100),2) if dis else None

    # Documents
    aadhaar = str(random.randint(2,9)) + rd(11)
    pan     = (ru(5)+rd(4)+ru(1)) if income>0 or random.random()<0.40 else None
    voter   = (ru(3)+rd(7)) if age>=18 else None
    passport= ra(8) if random.random()<0.12 else None
    dl      = ra(15) if age>=18 and random.random()<0.35 else None

    # Bank
    bname, bcode = random.choice(BANKS)
    bank_acc = rd(random.randint(10,16))
    ifsc     = bcode + "0" + rd(6)
    acc_type = random.choice(["Savings","Current","Jan Dhan",""])

    # Vehicle
    vtype   = random.choice(VEH_TYPES)
    vreg    = None; vfuel=None; vchas=None; vins=None; vcnt=0
    if vtype != "None":
        vcnt  = random.randint(1,2)
        sc    = "KL" if state=="Kerala" else state[:2].upper()
        vreg  = f"{sc}{random.randint(1,99):02d}{ru(2)}{rd(4)}"
        vfuel = random.choice(FUEL_TYPES[:4])
        vchas = ra(17)
        vins  = ra(15)

    # Property / land
    pcnt  = random.randint(0,3)
    preg  = ra(12) if pcnt>0 else None
    lreg  = ra(12) if random.random()<0.40 else None
    lpar  = f"{rd(4)}/{rd(3)}" if lreg else None

    # Ration
    rtype = random.choices(RAT_TYPES, weights=RAT_W)[0]
    rno   = ra(12) if rtype else None
    rpres = "Present" if rno else "Absent"

    # Contact
    mobile  = str(random.randint(6,9)) + rd(9)
    mob_usr = (str(random.randint(6,9))+rd(9)) if random.random()<0.70 else None
    email_v = f"{fname.lower()}{random.randint(1,999)}@{random.choice(['gmail.com','yahoo.com','rediffmail.com'])}" if random.random()<0.40 else None

    # Health
    hi_type = random.choice(HI_TYPES)
    hi_no   = ra(12) if hi_type!="None" else None
    abha    = rd(14) if random.random()<0.30 else None

    # Utilities
    kseb_no = rd(11) if random.random()<0.70 else None
    kwa_no  = rd(10) if random.random()<0.60 else None
    lpg_no  = rd(17) if random.random()<0.65 else None
    lpg_typ = random.choice(LPG_TYPES) if lpg_no else None
    kseb_ti = random.choice(KSEB_TIERS) if kseb_no else None
    kwa_st  = random.choice(["Active","Inactive",""]) if kwa_no else None

    # Kerala IDs
    spid      = "SP" + rd(10)
    family_id = "FAM" + rd(10)
    spark     = ("SPRK"+rd(8)) if is_govt else None
    pravasi   = ra(12) if random.random()<0.08 else None
    prav_st   = random.choice(["Active","Inactive","Returned"]) if pravasi else None
    uhid      = ("UHID"+rd(10)) if random.random()<0.50 else None
    smart_id  = ("SMT"+rd(10)) if random.random()<0.30 else None

    # Scheme IDs
    nregs     = ra(16) if random.random()<0.25 else None
    mnr_st    = random.choice(["Active","Inactive","Pending"]) if nregs else None
    pension_d = ra(12) if age>=60 and random.random()<0.40 else None
    pen_st    = random.choice(["Active","Inactive","Applied"]) if pension_d else None
    farmer_id = ("FARM"+rd(10)) if occ=="Farmer" and random.random()<0.70 else None
    pm_kisan  = rd(12) if farmer_id else None
    minority  = religion in ["Muslim","Christian"] and category in ["OBC","SC"]
    min_cert  = ra(12) if minority and random.random()<0.60 else None
    labour_c  = ra(12) if occ in ["Daily Wage Worker","Construction Worker","Migrant Worker"] and random.random()<0.50 else None
    lab_st    = random.choice(["Active","Inactive","Expired"]) if labour_c else None
    rsby_no   = rd(9) if random.random()<0.15 else None
    rsby_st   = random.choice(["Active","Inactive"]) if rsby_no else None
    abha_st   = random.choice(["Active","Inactive"]) if abha else None
    uan       = rd(12) if emp_typ in ["Government","Private","Semi-Government"] else None
    caste_c   = ra(12) if category in ["SC","ST","OBC"] else None
    child_n   = (random.choice(MALE_FIRST+FEMALE_FIRST)+" "+lname) if fam_cnt>2 and marital=="Married" else None
    drc       = ra(14) if random.random()<0.40 else None
    emp_id    = ra(12) if is_govt else None
    udise     = rd(11) if matric else None
    stud_id   = ra(12) if age<25 and matric else None
    schol_id  = ra(12) if random.random()<0.05 else None

    created  = f"2023-{random.randint(1,12):02d}-{random.randint(1,28):02d} {random.randint(0,23):02d}:{random.randint(0,59):02d}:00"
    is_activ = 1 if random.random()<0.95 else 0

    return (
        # 1-4: name parts
        name, fname, mname or None, lname,
        # 5-8: bio
        dob, age, gender, None,
        # 9-14: address
        village, f"{addr1}, {addr2}, {addr3}, {state} - {pincode}", None,
        addr1, addr2, addr3,
        # 15-19: location
        state, state_lgd, dist, dist_lgd, pincode,
        # 20-24: sub-location
        taluk, rd(5), village, rd(6), None,
        # 25-30: contact
        email_v, mobile, "AAD"+aadhaar, maybe(ra(12),0.30), None, mob_usr,
        # 31-41: employment
        child_n, emp_st, emp_typ or None, occ, 1 if is_govt else 0,
        income, maybe(ra(10),0.6) if income>0 else None, pension_d,
        1 if inc_tax else 0, maybe(ra(12),0.10), None,
        # 42-59: IDs
        spid, "SP"+rd(10), "SP"+rd(10), family_id, udise, spark,
        uhid, stud_id, pravasi, smart_id, schol_id, nregs,
        maybe(ra(12),0.05), maybe(ra(12),0.03), maybe(ra(12),0.03),
        rsby_no, abha, maybe(ra(14),0.20),
        # 60-73: family & identity
        father_n, mother_n, spouse, marital,
        ra(12) if marital=="Married" else None,
        fam_cnt, religion, category, CAT_ID.get(category,"CAT06"),
        caste, ra(6), caste_c, state, ra(12),
        # 74-87: disability & education
        1 if dis else 0, ra(14) if dis else None, dtype,
        ra(6) if dis else None, dpct, None,
        1 if matric else 0, 1 if twlft else 0,
        1 if grad else 0, 1 if pg else 0,
        # 84-87: farmer / minority
        farmer_id, pm_kisan, min_cert, 1 if minority else 0,
        # 88-97: vehicle
        vtype if vtype!="None" else None, vreg, vcnt,
        vins, preg, pcnt, lreg, vfuel, vchas, lpar,
        # 98-107: documents & finance
        pan, rno, rtype or None,
        bank_acc, bname, ifsc, uan,
        labour_c, hi_no, None,
        # 108-116: more IDs
        passport, dl, voter,
        kseb_no, kwa_no, lpg_no, drc, None, emp_id,
        # 117-124: admin
        random.randint(1,100), is_activ, 0,
        "system", created, None, None, created,
        # 125-137: status fields
        pen_st, mnr_st, rpres, lab_st, prav_st, None,
        rsby_st, abha_st, hi_type if hi_type!="None" else None,
        acc_type or None, kseb_ti, kwa_st, lpg_typ,
    )

# ── Database setup ────────────────────────────────────────────────────────────
print(f"Creating {DB_PATH} ...")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

conn = sqlite3.connect(DB_PATH)
cur  = conn.cursor()
cur.executescript("""
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA cache_size   = -65536;
PRAGMA temp_store   = MEMORY;
""")

cur.execute("""
CREATE TABLE citizen_master_records (
    unified_id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    name                                TEXT,
    first_name                          TEXT,
    middle_name                         TEXT,
    last_name                           TEXT,
    date_of_birth                       TEXT,
    age                                 INTEGER,
    gender                              TEXT,
    photo                               TEXT,
    place_of_birth                      TEXT,
    address_permanent                   TEXT,
    address_temporary                   TEXT,
    address_line1                       TEXT,
    address_line2                       TEXT,
    address_line3                       TEXT,
    state                               TEXT,
    state_lgd_code                      TEXT,
    district                            TEXT,
    district_lgd_code                   TEXT,
    pincode                             TEXT,
    taluk                               TEXT,
    taluk_lgd_code                      TEXT,
    village                             TEXT,
    village_lgd_code                    TEXT,
    urban_rural_lb_id                   TEXT,
    email                               TEXT,
    mobile_aadhaar                      TEXT,
    aadhaar_ref_no                      TEXT,
    birth_certificate_no                TEXT,
    death_certificate_no                TEXT,
    mobile_user                         TEXT,
    child_name                          TEXT,
    employment_status                   TEXT,
    employment_type                     TEXT,
    occupation                          TEXT,
    govt_employee_flag                  INTEGER,
    individual_income                   REAL,
    income_certificate_no               TEXT,
    pension_details                     TEXT,
    income_tax_payee                    INTEGER,
    trade_license                       TEXT,
    manufacturer_license                TEXT,
    spid                                TEXT,
    father_spid                         TEXT,
    mother_spid                         TEXT,
    family_id                           TEXT,
    udise_samagra_id                    TEXT,
    spark_id                            TEXT,
    uhid                                TEXT,
    student_id                          TEXT,
    pravasi_id                          TEXT,
    smart_id                            TEXT,
    scholarship_id                      TEXT,
    nregs_job_card_mnrega_no            TEXT,
    adithi_id                           TEXT,
    kpsc_candidate_id                   TEXT,
    ex_servicemen_id                    TEXT,
    rashtriya_swasthya_bima_yojana_no   TEXT,
    ayushman_bharat_health_account_no   TEXT,
    apaar_id                            TEXT,
    father_name                         TEXT,
    mother_name                         TEXT,
    spouse_name                         TEXT,
    marital_status                      TEXT,
    marriage_certificate_no             TEXT,
    family_member_count                 INTEGER,
    religion                            TEXT,
    category                            TEXT,
    category_id                         TEXT,
    caste                               TEXT,
    caste_id                            TEXT,
    caste_certificate_no                TEXT,
    domicile                            TEXT,
    domicile_certificate_no             TEXT,
    disability_flag                     INTEGER,
    udid_no                             TEXT,
    disability_type                     TEXT,
    disability_type_id                  TEXT,
    disability_percentage               REAL,
    ug_diploma                          TEXT,
    matriculate                         INTEGER,
    twelfth                             INTEGER,
    graduate                            INTEGER,
    post_graduate                       INTEGER,
    farmer_id                           TEXT,
    pm_kisan_id                         TEXT,
    minority_certificate                TEXT,
    minority_flag                       INTEGER,
    vehicle_type                        TEXT,
    vehicle_registration_no             TEXT,
    vehicle_count                       INTEGER,
    vehicle_insurance_certificate       TEXT,
    property_registration_no            TEXT,
    property_count                      INTEGER,
    land_registration_no                TEXT,
    vehicle_fuel_type                   TEXT,
    vehicle_chassis_no                  TEXT,
    land_parcel                         TEXT,
    pan_no                              TEXT,
    ration_card_no                      TEXT,
    ration_card_type                    TEXT,
    bank_account_no                     TEXT,
    bank_name                           TEXT,
    ifsc_code                           TEXT,
    uan_number                          TEXT,
    labour_card                         TEXT,
    health_insurance_no                 TEXT,
    pacs_id                             TEXT,
    passport_no                         TEXT,
    driving_license_no                  TEXT,
    voter_id                            TEXT,
    kseb_consumer_no                    TEXT,
    kwa_consumer_no                     TEXT,
    lpg_consumer_no                     TEXT,
    digital_revenue_card_number         TEXT,
    parichayid                          TEXT,
    empid                               TEXT,
    batch_id                            INTEGER,
    is_active                           INTEGER NOT NULL DEFAULT 1,
    is_deleted                          INTEGER NOT NULL DEFAULT 0,
    created_by                          TEXT,
    created_on                          TEXT,
    modified_by                         TEXT,
    modified_on                         TEXT,
    processed_at                        TEXT,
    pension_status                      TEXT,
    mgnrega_status                      TEXT,
    ration_card_presence                TEXT,
    labour_card_status                  TEXT,
    pravasi_status                      TEXT,
    migrant_labour_status               TEXT,
    rsby_status                         TEXT,
    abha_status                         TEXT,
    health_insurance_type               TEXT,
    bank_account_type                   TEXT,
    kseb_usage_tier                     TEXT,
    kwa_status                          TEXT,
    lpg_type                            TEXT
);
""")
conn.commit()

# ── Generate records ──────────────────────────────────────────────────────────
COLS = 137   # columns after unified_id (AUTOINCREMENT)
SQL  = "INSERT INTO citizen_master_records VALUES (NULL," + ",".join(["?"]*COLS) + ")"

print(f"Generating {TOTAL:,} records ...")
t0 = time.time()
done = 0

while done < TOTAL:
    size = min(BATCH, TOTAL - done)
    rows = [gen() for _ in range(size)]
    # Validate column count on first batch
    if done == 0:
        got = len(rows[0])
        if got != COLS:
            print(f"\nERROR: gen() returned {got} values, expected {COLS}. Fix the script.")
            conn.close(); sys.exit(1)
    cur.executemany(SQL, rows)
    conn.commit()
    done += size
    elapsed = time.time() - t0
    rate    = done / elapsed
    eta     = (TOTAL - done) / rate if rate else 0
    pct     = done / TOTAL * 100
    print(f"\r  {done:>10,}/{TOTAL:,} ({pct:5.1f}%)  {rate:,.0f} rec/s  ETA {eta:.0f}s  ", end="", flush=True)

# ── Indexes (built after insert — much faster) ────────────────────────────────
print("\n\nBuilding indexes ...")
for idx_sql in [
    "CREATE INDEX idx_state        ON citizen_master_records(state);",
    "CREATE INDEX idx_district      ON citizen_master_records(district);",
    "CREATE INDEX idx_gender        ON citizen_master_records(gender);",
    "CREATE INDEX idx_age           ON citizen_master_records(age);",
    "CREATE INDEX idx_religion      ON citizen_master_records(religion);",
    "CREATE INDEX idx_category      ON citizen_master_records(category);",
    "CREATE INDEX idx_emp_status    ON citizen_master_records(employment_status);",
    "CREATE INDEX idx_marital       ON citizen_master_records(marital_status);",
    "CREATE INDEX idx_occupation    ON citizen_master_records(occupation);",
    "CREATE INDEX idx_disability    ON citizen_master_records(disability_flag);",
    "CREATE INDEX idx_govt_emp      ON citizen_master_records(govt_employee_flag);",
    "CREATE INDEX idx_minority      ON citizen_master_records(minority_flag);",
    "CREATE INDEX idx_is_active     ON citizen_master_records(is_active);",
    "CREATE INDEX idx_state_dist    ON citizen_master_records(state, district);",
]:
    cur.execute(idx_sql)
conn.commit()
conn.close()

elapsed = time.time() - t0
size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
print(f"Done! {TOTAL:,} records in {elapsed:.1f}s  →  {DB_PATH}  ({size_mb:.0f} MB)")
