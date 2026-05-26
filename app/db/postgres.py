import asyncpg

from app.config import settings

# Глобальный пул соединений
_pool: asyncpg.Pool | None = None

CREATE_TABLES_SQL = """
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- L1: Сырые события
CREATE TABLE IF NOT EXISTS l1_raw_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_agent VARCHAR(64) NOT NULL,
    domain VARCHAR(64) NOT NULL,
    raw_payload JSONB NOT NULL,
    processed_to_l2 BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_l1_timestamp ON l1_raw_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_l1_domain ON l1_raw_events(domain, timestamp);
CREATE INDEX IF NOT EXISTS idx_l1_agent ON l1_raw_events(source_agent);
CREATE INDEX IF NOT EXISTS idx_l1_processed ON l1_raw_events(processed_to_l2, timestamp);

-- L2: Дневные буферы
CREATE TABLE IF NOT EXISTS l2_daily_buffers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    date DATE NOT NULL,
    domain VARCHAR(64) NOT NULL,
    summary JSONB NOT NULL,
    source_event_ids UUID[] NOT NULL DEFAULT '{}',
    confidence FLOAT DEFAULT 0.0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_l2_date_domain ON l2_daily_buffers(date, domain);

-- L3: Эталонные знания
CREATE TABLE IF NOT EXISTS l3_master_knowledge (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    domain VARCHAR(64) NOT NULL,
    knowledge_type VARCHAR(32) NOT NULL CHECK (knowledge_type IN ('pattern', 'mistake', 'rule')),
    content JSONB NOT NULL,
    version INT DEFAULT 1,
    derived_from_l2_ids UUID[] DEFAULT '{}',
    related_tool_ids UUID[] DEFAULT '{}',
    effective_from TIMESTAMPTZ DEFAULT NOW(),
    effective_to TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_l3_active ON l3_master_knowledge(domain, knowledge_type) WHERE effective_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_l3_domain ON l3_master_knowledge(domain) WHERE effective_to IS NULL;

-- pgvector колонка для семантического поиска (384-dim multilingual-e5-small)
ALTER TABLE l3_master_knowledge ADD COLUMN IF NOT EXISTS embedding vector(384);
CREATE INDEX IF NOT EXISTS idx_l3_knowledge_hnsw ON l3_master_knowledge
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- L3: Реестр инструментов
CREATE TABLE IF NOT EXISTS l3_tools_registry (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    domain VARCHAR(64) NOT NULL,
    tool_name VARCHAR(128) NOT NULL,
    tool_type VARCHAR(32) CHECK (tool_type IN ('api', 'script', 'prompt', 'library', 'service')),
    description TEXT,
    config_schema JSONB,
    usage_patterns JSONB,
    l2_source_ids UUID[] DEFAULT '{}',
    version INT DEFAULT 1,
    effective_from TIMESTAMPTZ DEFAULT NOW(),
    effective_to TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_l3_tools_active ON l3_tools_registry(domain, tool_type) WHERE effective_to IS NULL;

-- pgvector колонка для tools
ALTER TABLE l3_tools_registry ADD COLUMN IF NOT EXISTS embedding vector(384);
CREATE INDEX IF NOT EXISTS idx_l3_tools_hnsw ON l3_tools_registry
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- L4: Снапшоты
CREATE TABLE IF NOT EXISTS l4_snapshots (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    snapshot_time TIMESTAMPTZ DEFAULT NOW(),
    snapshot_type VARCHAR(8) CHECK (snapshot_type IN ('full', 'delta')),
    delta_base_id UUID REFERENCES l4_snapshots(id),
    l3_knowledge_snapshot JSONB,
    l3_tools_snapshot JSONB,
    total_knowledge_records INT DEFAULT 0,
    total_tools INT DEFAULT 0,
    changed_knowledge_records INT DEFAULT 0,
    changed_tools INT DEFAULT 0,
    snapshot_hash VARCHAR(128),
    s3_path VARCHAR(255),
    is_verified BOOLEAN DEFAULT FALSE,
    comment TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- L5: Аудит
CREATE TABLE IF NOT EXISTS l5_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    event_time TIMESTAMPTZ DEFAULT NOW(),
    agent_id VARCHAR(64),
    action VARCHAR(64) CHECK (action IN (
        'event_ingest', 'daily_consolidate', 'weekly_consolidate',
        'snapshot_create', 'restore', 'cleanup', 'validation_error',
        'auth_failure', 'monthly_audit', 'operative_query',
        'operative_close', 'feedback'
    )),
    target_table VARCHAR(64),
    target_id UUID,
    details JSONB,
    ip_address VARCHAR(45),
    success BOOLEAN DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON l5_audit_log(event_time);
CREATE INDEX IF NOT EXISTS idx_audit_agent ON l5_audit_log(agent_id, event_time);
CREATE INDEX IF NOT EXISTS idx_audit_action ON l5_audit_log(action, event_time);

-- Per-agent persistent state (checkpoint для recovery после срыва сессии / окончания токенов)
CREATE TABLE IF NOT EXISTS agent_states (
    agent_id VARCHAR(64) PRIMARY KEY,
    current_task TEXT,
    state_data JSONB DEFAULT '{}',
    active_session_ids UUID[] DEFAULT '{}',
    last_checkpoint_at TIMESTAMPTZ DEFAULT NOW(),
    total_events INT DEFAULT 0,
    total_checkpoints INT DEFAULT 0,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    -- Multi-agent collaboration fields (sprint v0.5.0-prod #3 + v0.5.5)
    project VARCHAR(64),
    machine VARCHAR(128),
    capabilities JSONB DEFAULT '[]',
    last_heartbeat_at TIMESTAMPTZ DEFAULT NOW()
);
-- Idempotent migration for existing rows
ALTER TABLE agent_states ADD COLUMN IF NOT EXISTS project VARCHAR(64);
ALTER TABLE agent_states ADD COLUMN IF NOT EXISTS machine VARCHAR(128);
ALTER TABLE agent_states ADD COLUMN IF NOT EXISTS capabilities JSONB DEFAULT '[]';
ALTER TABLE agent_states ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ DEFAULT NOW();
CREATE INDEX IF NOT EXISTS idx_agent_states_updated ON agent_states(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_states_project_heartbeat ON agent_states(project, last_heartbeat_at DESC);

-- Per-agent API keys (sprint v0.5.0-prod #3): each agent gets its own key,
-- can be revoked, audited via last_used_at. Replaces single-shared key from .env.
CREATE TABLE IF NOT EXISTS agent_keys (
    api_key VARCHAR(128) PRIMARY KEY,
    agent_id VARCHAR(64) NOT NULL REFERENCES agent_states(agent_id) ON DELETE CASCADE,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_agent_keys_agent ON agent_keys(agent_id) WHERE revoked_at IS NULL;

-- История checkpoints (для отката)
CREATE TABLE IF NOT EXISTS agent_state_history (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_id VARCHAR(64) NOT NULL,
    current_task TEXT,
    state_data JSONB,
    active_session_ids UUID[],
    checkpoint_at TIMESTAMPTZ DEFAULT NOW(),
    trigger VARCHAR(32) CHECK (trigger IN ('manual', 'auto', 'heartbeat', 'session_close', 'event_milestone'))
);
CREATE INDEX IF NOT EXISTS idx_agent_state_history_agent ON agent_state_history(agent_id, checkpoint_at DESC);

-- Таблица арбитража
CREATE TABLE IF NOT EXISTS l_arbitration (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    domain VARCHAR(64) NOT NULL,
    conflict_description JSONB,
    proposed_knowledge_id UUID REFERENCES l3_master_knowledge(id),
    existing_knowledge_id UUID REFERENCES l3_master_knowledge(id),
    status VARCHAR(32) CHECK (status IN ('pending', 'resolved_new_wins', 'resolved_old_wins', 'merged')),
    resolved_by VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);
"""


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=2,
            max_size=10,
        )
    return _pool


async def init_db() -> None:
    """Создаёт таблицы при старте приложения."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLES_SQL)


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
