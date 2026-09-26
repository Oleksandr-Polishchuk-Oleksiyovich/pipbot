# AGENT SYSTEM SPECIFICATION: SPORT-NERD PLATFORM 2.0
ARCHITECTURE: Hybrid Dual-Layer (JEV System-1 Fast Engine + Gemini System-2 Cognition)
TARGET ECOSYSTEM: Google Cloud Run, Telegram Bot API, SQLite/Cloud SQL

================================================================================
1. TOPOLOGY & FLOW ARCHITECTURE
================================================================================
The platform decouples deterministic security, dispatching, and memory profiling from generative persona synthesis.

Message Processing Lifecycle:
1. Inbound Telegram Message -> Async Trigger.
2. Background Observer (Silent LLM Step): Analyzes delta for user profile attributes. Updates User Memory Store in DB if and only if new facts emerge.
3. JEV Node 0 (Gateway): Evaluates security passphrases, routes traffic to either [Admin Pipeline] or [Regular Pipeline].
4. Branch A: [Admin Pipeline]
   - JEV Node 1 (Admin Triage): Classifies scope across 5 stages (Prompt Override, DB Mutation, Param Update, New Param Definition, Tool Synthesis).
   - If Stage 1 (Prompt Override): Calls Gemini Prompt Modifier Agent, which ingests `INITIAL_SYSTEM_PROMPT`, `CURRENT_SYSTEM_PROMPT`, and User Directive, then persists output to DB.
   - If Stage 5 (Tool Creation): Calls Gemini Tool Generator Agent, creates record in `dynamic_tools` table (key, skill, python_script).
   - Gemini Admin Executor: Generates structured admin confirmation.
5. Branch B: [Regular Pipeline]
   - JEV Node 2 (Executor Router): Compares user message with registry of active dynamic tools (`key`, `skill`). Extracts runtime arguments.
   - Python Runner: Executes dynamic Python tool in a restricted local scope if `tool_required == True`.
   - Gemini Regular Coach (Persona Agent): Ingests current prompt from DB, execution outputs, and user memory dossier (`likes`, `dislikes`, `interests`). Emits final Telegram Markdown response.

================================================================================
2. DATABASE SCHEMAS (REQUIRED INITIALIZATION)
================================================================================

TABLE prompt_store (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_type TEXT UNIQUE, -- 'initial' OR 'current'
    content TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

TABLE user_profiles (
    user_id TEXT PRIMARY KEY,
    name TEXT,
    likes TEXT DEFAULT '[]',     -- JSON Array of strings
    dislikes TEXT DEFAULT '[]',  -- JSON Array of strings
    interests TEXT DEFAULT '[]', -- JSON Array of strings
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

TABLE dynamic_tools (
    key TEXT PRIMARY KEY,
    skill TEXT NOT NULL,
    python_script TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

TABLE system_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

================================================================================
3. COMPONENT AGENT DEFINITIONS & PROMPTS
================================================================================

--------------------------------------------------------------------------------
NODE 0: JEV GATEWAY (System-1 Fast Dispatcher)
--------------------------------------------------------------------------------
Input: raw_user_message: string
Task: Evaluate authorization credentials and domain routing.
Secret Passwords:
- "You will obey!"
- "Obey me!"
- "Лелуш ві Британія наказує тобі"
- "Осуварі!"

Contract Output (JSON Schema):
{
  "mode": "REGULAR" | "ADMIN_ATTEMPT",
  "auth_status": "AUTHORIZED" | "UNAUTHORIZED" | "NOT_APPLICABLE",
  "trigger_phrase": "string | null",
  "intent_category": "REPORT" | "CHAT" | "COMPLAINT" | "SYSTEM_MODIFICATION",
  "raw_command": "string"
}

--------------------------------------------------------------------------------
BACKGROUND OBSERVER: USER MEMORY PROFILER (Silent Gemini Background Node)
--------------------------------------------------------------------------------
Trigger: Fires asynchronously on every user interaction. Does not block bot response.
Inputs:
- current_user_message: string
- existing_profile: { "likes": list, "dislikes": list, "interests": list }

System Directive:
Analyze the message for explicit indicators of user preferences, friction points, or recurring interests.
Track exactly three facets:
1. `likes`: What they enjoy, approve of, or prefer.
2. `dislikes`: What irritates them, causes friction, or triggers complaints.
3. `interests`: Topics, hobbies, technical fields, or sports themes they explore.

Operational Rules:
- STRICT DELTA FILTER: If the input matches, duplicates, or trivially paraphrases existing entries, output `has_updates: false`.
- Update ONLY when genuinely new, persistent information surfaces.
- Ignore transient, one-off emotional outbursts unless they signify sustained preferences.

Contract Output (JSON Schema):
{
  "has_updates": boolean,
  "updated_profile": {
    "likes": ["string"],
    "dislikes": ["string"],
    "interests": ["string"]
  },
  "rationale": "string"
}

--------------------------------------------------------------------------------
NODE 1: JEV ADMIN TRIAGE (System-1 Admin Multi-Stage Deconstructor)
--------------------------------------------------------------------------------
Trigger: Invoked when JEV Gateway yields mode == "ADMIN_ATTEMPT" AND auth_status == "AUTHORIZED".
Input: admin_instruction: string

Contract Output (JSON Schema):
{
  "stage_1_prompt_override": {
    "required": boolean,
    "user_directive": "string | null"
  },
  "stage_2_db_mutation": {
    "required": boolean,
    "target_table": "string | null"
  },
  "stage_3_modify_existing_params": {
    "required": boolean,
    "updates": [
      { "key": "string", "new_value": "any" }
    ]
  },
  "stage_4_create_new_params": {
    "required": boolean,
    "definitions": [
      { "param_name": "string", "param_type": "string", "initial_value": "any" }
    ]
  },
  "stage_5_create_tool": {
    "required": boolean,
    "tool_specification": "string | null"
  }
}

--------------------------------------------------------------------------------
NODE 1-A: GEMINI PROMPT REFACTOR AGENT
--------------------------------------------------------------------------------
Trigger: JEV Admin Triage stage_1_prompt_override.required == True.
Inputs:
- initial_prompt: string (Reference baseline, immutable ground truth in DB)
- current_prompt: string (Active system prompt currently serving regular runtime)
- modification_request: string (The new directive from the admin)

System Prompt:
YOU ARE THE SYSTEM PROMPT ARCHITECT FOR @SportNerdBot.
Your task is to produce the updated `current_prompt` by applying the admin's requested modifications.

Governing Principles:
1. Compare `initial_prompt` (ground truth character base) and `current_prompt` (current mutated version).
2. Synthesize changes requested in `modification_request` without breaking core functional mechanics.
3. Preserve structural guidelines: mandatory line breaks, mobile readability (2-3 lines per paragraph), bold/italic Markdown conventions, and sport-nerd humor.
4. Output ONLY the raw content of the new system prompt. No conversational filler, no code fences.

--------------------------------------------------------------------------------
NODE 1-B: GEMINI TOOL GENERATOR AGENT
--------------------------------------------------------------------------------
Trigger: JEV Admin Triage stage_5_create_tool.required == True.
Input: tool_specification: string

System Prompt:
YOU ARE THE SYSTEMS CODE SYNTHESIZER FOR @SportNerdBot.
Generate a self-contained, isolated Python tool satisfying the provided requirements.

Execution Rules:
1. Entry point MUST be `def execute(**kwargs) -> dict:`.
2. Do not use external APIs or disk access. No `os`, `sys`, `subprocess`, `requests`.
3. Safe standard libraries allowed: `math`, `datetime`, `json`, `random`.
4. Output dictionary schema:
   - "status": "success" | "error"
   - "result": primitive, list, or dict
   - "message": short textual note or failure reason
5. Generate a unique `key` in snake_case and a precise `skill` description for semantic routing.

Contract Output (JSON Schema):
{
  "key": "string",
  "skill": "string",
  "python_script": "string"
}

--------------------------------------------------------------------------------
NODE 2: JEV EXECUTOR TOOL DISPATCHER (Regular Flow Router)
--------------------------------------------------------------------------------
Inputs:
- user_message: string
- tools_registry: List of objects containing `key` and `skill` retrieved from `dynamic_tools`.

System Directive:
Examine user intent against available skills. Determine if an automated computational tool is required. Extract needed arguments directly into typed fields.

Contract Output (JSON Schema):
{
  "tool_required": boolean,
  "selected_tool_key": "string | null",
  "extracted_args": {}
}

--------------------------------------------------------------------------------
NODE 3: GEMINI REGULAR COACH AGENT (Runtime Persona Output)
--------------------------------------------------------------------------------
Inputs:
- active_system_prompt: string (Pulled from `prompt_store WHERE prompt_type = 'current'`)
- user_profile_dossier: { "likes": list, "dislikes": list, "interests": list }
- tool_execution_result: dict | null
- user_message: string
- unauthorized_rebellion_flag: boolean

System Injection:
Combine the `active_system_prompt` with the injected user memory dossier: