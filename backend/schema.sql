CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name VARCHAR(120),
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Kolkata',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS conditions (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(160) NOT NULL UNIQUE,
    category VARCHAR(100),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id BIGSERIAL PRIMARY KEY,
    name_en VARCHAR(160) NOT NULL,
    name_hi VARCHAR(160),
    category VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS interactions (
    id BIGSERIAL PRIMARY KEY,
    condition_id BIGINT NOT NULL REFERENCES conditions(id) ON DELETE CASCADE,
    item_id BIGINT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    risk_level VARCHAR(32) NOT NULL CHECK (risk_level IN ('Low', 'Moderate', 'High')),
    gap_minutes INTEGER NOT NULL DEFAULT 0 CHECK (gap_minutes >= 0 AND gap_minutes <= 1440),
    advice TEXT NOT NULL,
    CONSTRAINT uq_condition_item UNIQUE (condition_id, item_id)
);

CREATE TABLE IF NOT EXISTS diary_logs (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    condition_id BIGINT REFERENCES conditions(id) ON DELETE SET NULL,
    item_id BIGINT REFERENCES items(id) ON DELETE SET NULL,
    meal_note VARCHAR(255) NOT NULL,
    intake_time VARCHAR(32) NOT NULL DEFAULT 'Not specified',
    gap_minutes INTEGER NOT NULL DEFAULT 0 CHECK (gap_minutes >= 0 AND gap_minutes <= 1440),
    symptom VARCHAR(160) NOT NULL DEFAULT '',
    severity VARCHAR(20) NOT NULL DEFAULT 'None' CHECK (severity IN ('None', 'Mild', 'Moderate', 'Severe')),
    notes TEXT NOT NULL DEFAULT '',
    logged_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_items_name_en ON items (LOWER(name_en));
CREATE INDEX IF NOT EXISTS idx_items_name_hi ON items (LOWER(name_hi));
CREATE INDEX IF NOT EXISTS idx_diary_user_date ON diary_logs (user_id, logged_date DESC);
CREATE INDEX IF NOT EXISTS idx_diary_user_created ON diary_logs (user_id, created_at DESC);