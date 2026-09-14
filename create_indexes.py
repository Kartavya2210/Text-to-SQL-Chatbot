from sqlalchemy import create_engine, text

engine = create_engine("sqlite:///citizen_data.db")

indexes = [
    ("state",      "CREATE INDEX IF NOT EXISTS idx_state      ON citizen_master_records(state)"),
    ("district",   "CREATE INDEX IF NOT EXISTS idx_district   ON citizen_master_records(district)"),
    ("gender",     "CREATE INDEX IF NOT EXISTS idx_gender     ON citizen_master_records(gender)"),
    ("age",        "CREATE INDEX IF NOT EXISTS idx_age        ON citizen_master_records(age)"),
    ("income",     "CREATE INDEX IF NOT EXISTS idx_income     ON citizen_master_records(individual_income)"),
    ("disability", "CREATE INDEX IF NOT EXISTS idx_disability ON citizen_master_records(disability_flag)"),
    ("religion",   "CREATE INDEX IF NOT EXISTS idx_religion   ON citizen_master_records(religion)"),
    ("category",   "CREATE INDEX IF NOT EXISTS idx_category   ON citizen_master_records(category)"),
]

with engine.connect() as conn:
    for name, sql in indexes:
        print(f"Creating index on {name}...")
        conn.execute(text(sql))
        conn.commit()

print("All indexes created.")
