-- ============================================================
-- harrisonfd-dispatch — PostgreSQL schema
-- Run as: psql -U your_db_user -d dispatch -h localhost -f schema.sql
-- ============================================================

DO $$ BEGIN
    CREATE TYPE priority_level AS ENUM ('routine', 'elevated', 'high');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS transmissions (
    id              SERIAL PRIMARY KEY,
    recorded_at     TIMESTAMPTZ     NOT NULL,
    duration_sec    NUMERIC(8,2)    NOT NULL DEFAULT 0,
    wav_filename    TEXT            NOT NULL,
    wav_path        TEXT            NOT NULL,
    transcript      TEXT,
    whisper_model   VARCHAR(32),
    priority        priority_level,
    llm_summary     TEXT,
    analyzed        BOOLEAN         NOT NULL DEFAULT FALSE,
    rms_amplitude   NUMERIC(8,6),
    max_amplitude   NUMERIC(8,6),
    mean_norm       NUMERIC(8,6),
    rough_frequency INTEGER,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tx_recorded_at ON transmissions (recorded_at DESC);
CREATE INDEX IF NOT EXISTS idx_tx_priority    ON transmissions (priority);
CREATE INDEX IF NOT EXISTS idx_tx_analyzed    ON transmissions (analyzed);
CREATE INDEX IF NOT EXISTS idx_tx_wav_path    ON transmissions (wav_path);
CREATE INDEX IF NOT EXISTS idx_tx_rms         ON transmissions (rms_amplitude);
CREATE INDEX IF NOT EXISTS idx_tx_fts
    ON transmissions USING gin(to_tsvector('english', COALESCE(transcript, '')));

CREATE TABLE IF NOT EXISTS keyword_rules (
    id          SERIAL PRIMARY KEY,
    keyword     TEXT            NOT NULL UNIQUE,
    category    TEXT            NOT NULL,
    alert_level priority_level  NOT NULL DEFAULT 'elevated',
    is_active   BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alerts (
    id              SERIAL PRIMARY KEY,
    transmission_id INTEGER         NOT NULL REFERENCES transmissions(id) ON DELETE CASCADE,
    keyword_rule_id INTEGER         REFERENCES keyword_rules(id) ON DELETE SET NULL,
    keyword_matched TEXT            NOT NULL,
    alert_level     priority_level  NOT NULL,
    llm_response    TEXT,
    acknowledged    BOOLEAN         NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_tx_id  ON alerts (transmission_id);
CREATE INDEX IF NOT EXISTS idx_alerts_ack    ON alerts (acknowledged) WHERE acknowledged = FALSE;
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts (created_at DESC);

CREATE TABLE IF NOT EXISTS visitor_counts (
    id      SERIAL PRIMARY KEY,
    date    DATE    NOT NULL UNIQUE DEFAULT CURRENT_DATE,
    count   INTEGER NOT NULL DEFAULT 0
);

INSERT INTO visitor_counts (date, count) VALUES (CURRENT_DATE, 0) ON CONFLICT (date) DO NOTHING;

CREATE OR REPLACE FUNCTION total_visitors()
RETURNS INTEGER AS $$
    SELECT COALESCE(SUM(count), 0)::INTEGER FROM visitor_counts;
$$ LANGUAGE SQL;

CREATE OR REPLACE VIEW daily_summary AS
SELECT
    DATE(recorded_at AT TIME ZONE 'UTC')                    AS day,
    COUNT(*)                                                AS total_transmissions,
    COUNT(*) FILTER (WHERE priority = 'elevated')           AS elevated_count,
    COUNT(*) FILTER (WHERE priority = 'high')               AS high_count,
    COUNT(*) FILTER (WHERE analyzed = TRUE)                 AS analyzed_count,
    ROUND(AVG(duration_sec)::numeric, 2)                    AS avg_duration_sec,
    SUM(duration_sec)                                       AS total_duration_sec
FROM transmissions
GROUP BY DATE(recorded_at AT TIME ZONE 'UTC')
ORDER BY day DESC;

CREATE OR REPLACE VIEW live_stats AS
SELECT
    (SELECT COUNT(*) FROM transmissions WHERE recorded_at >= CURRENT_DATE)                              AS transmissions_today,
    (SELECT COUNT(*) FROM transmissions WHERE recorded_at >= CURRENT_DATE AND priority = 'elevated')    AS elevated_today,
    (SELECT COUNT(*) FROM transmissions WHERE recorded_at >= CURRENT_DATE AND priority = 'high')        AS high_today,
    (SELECT COUNT(*) FROM alerts WHERE acknowledged = FALSE)                                            AS unacked_alerts,
    (SELECT COUNT(*) FROM alerts WHERE created_at >= CURRENT_DATE)                                     AS alerts_today,
    (SELECT COUNT(*) FROM transmissions)                                                                AS total_all_time;

-- ── Seed keyword rules ────────────────────────────────────────
INSERT INTO keyword_rules (keyword, category, alert_level) VALUES
    ('structure fire',      'fire',     'high'),
    ('working fire',        'fire',     'high'),
    ('fully involved',      'fire',     'high'),
    ('house fire',          'fire',     'high'),
    ('brush fire',          'fire',     'elevated'),
    ('grass fire',          'fire',     'elevated'),
    ('smoke showing',       'fire',     'elevated'),
    ('explosion',           'fire',     'high'),
    ('hazmat',              'fire',     'high'),
    ('cardiac arrest',      'medical',  'high'),
    ('not breathing',       'medical',  'high'),
    ('unconscious',         'medical',  'high'),
    ('unresponsive',        'medical',  'high'),
    ('blood loss',          'medical',  'high'),
    ('heavy bleeding',      'medical',  'high'),
    ('stroke',              'medical',  'high'),
    ('chest pain',          'medical',  'elevated'),
    ('difficulty breathing','medical',  'elevated'),
    ('overdose',            'medical',  'high'),
    ('diabetic',            'medical',  'elevated'),
    ('motor vehicle accident','traffic','elevated'),
    ('mva',                 'traffic',  'elevated'),
    ('rollover',            'traffic',  'high'),
    ('entrapment',          'traffic',  'high'),
    ('vehicle fire',        'traffic',  'high'),
    ('head on',             'traffic',  'high'),
    ('pedestrian struck',   'traffic',  'high'),
    ('shooting',            'police',   'high'),
    ('shots fired',         'police',   'high'),
    ('stabbing',            'police',   'high'),
    ('officer down',        'police',   'high'),
    ('officer needs assistance','police','high'),
    ('active shooter',      'police',   'high'),
    ('armed subject',       'police',   'high'),
    ('police on scene',     'police',   'elevated'),
    ('pursuit',             'police',   'elevated'),
    ('domestic',            'police',   'elevated'),
    ('gas leak',            'utility',  'high'),
    ('power line down',     'utility',  'elevated'),
    ('water main',          'utility',  'elevated'),
    ('building collapse',   'utility',  'high')
ON CONFLICT (keyword) DO NOTHING;
