# SYSTEM SPECIFICATION: SPORT-NERD PLATFORM (LANGUAGE-AGNOSTIC ARCHITECTURE)

## 1. ARCHITECTURAL OVERVIEW
The system implements a **Dual-Layer Cognitive Architecture** decoupling fast deterministic evaluation, validation, and state governance (System-1) from generative reasoning, synthesis, and persona execution (System-2).

```
                      +-----------------------------+
                      |   Inbound Message / Event   |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |   Node 0: Gateway & Auth    |
                      +--------------+--------------+
                                     |
                    +----------------+----------------+
                    |                                 |
         [Admin Auth Match]                  [Regular Traffic]
                    |                                 |
                    v                                 v
    +-------------------------------+   +-----------------------------+
    | Node 1: Admin Governance Loop |   | Node 3: Runtime Dispatcher  |
    | - JEV State Need Triage       |   | - Dynamic State Injection   |
    | - Iterative Consensus (N<=7)  |   | - Deterministic Tools Call  |
    | - Conflict & Novelty Analysis |   | - System-2 Persona Output   |
    | - Prompt Merger & Persist     |   +-----------------------------+
    +-------------------------------+
```

---

## 2. API ROLES & PROTOCOL CONTRACTS

### 2.1. System-1 Engine (JEV / Decision Engine API)
* **Role**: Deterministic gatekeeper, risk assessor, validator, and semantic judge. Does NOT produce long-form creative text or execute arbitrary code.
* **Responsibilities**:
  1. **Triage**: Classifies whether an incoming admin directive necessitates tracking new persistent state counters/variables (`needs_new_states`).
  2. **Proposal Review**: Scores candidate state schemas and operational instructions on a normalized `0.0 .. 1.0` scale against strict criteria (>= 0.90 approval threshold).
  3. **Conflict & Novelty Analysis**: Determines whether a new directive contradicts active system instructions and isolates newly introduced operational facts.
  4. **Contract Interface**: Standard JSON over HTTPS (`POST /api/v1/decisions`).
     - Types: `noul` (probability of true/yes in [0, 1]), `score` (ordered discrete qualitative levels), `choice` (categorical selection with confidence).

### 2.2. System-2 Engine (Gemini / Generative Cognition API)
* **Role**: Deep cognitive synthesizer, persona agent, and language refactorer.
* **Responsibilities**:
  1. **State Schema Proposal**: Converts natural language requests into structured key definitions, data types, initial values, and management guidelines.
  2. **Prompt Synthesis & Merge**: Ingests baseline character prompt, active dynamic overrides, JEV conflict analysis, and state definitions to compile a unified, non-contradictory prompt.
  3. **Interactive Persona Response**: Ingests live state data, message context, and formatting rules to produce engaging user-facing communication.
  4. **Tool Selection (Function Calling)**: Identifies user intent to invoke domain tools (e.g., progress verification, quota alterations).

---

## 3. CORE ABSTRACTION NODES

### NODE 0: Gateway & Authorization Dispatcher
* **Input**: Incoming raw user message payload, sender identity, timestamp.
* **Logic**:
  - Scans message against defined secret administrative passphrases (case-insensitive, substring search):
    `["obey", "you will obey", "лелуш", "осуварі"]`.
  - **Invariant**: Authority is grant-based per passphrase. Regardless of sender identity, presenting a valid passphrase activates Administrative Mode.
* **Routing**:
  - If passphrase matched: Routes to **Node 1 (Admin Governance Pipeline)**.
  - If no passphrase: Routes to **Node 3 (Regular Runtime Pipeline)**.

---

### NODE 1: Admin Governance Pipeline & Iterative Consensus Loop
Governed by a host-language agnostic controller loop.

```
Step 1: JEV Triage
        Input: Admin Directive, Existing State Keys
        Output: needs_new_states (boolean), confidence (float)

Step 2: State Consensus Loop (If needs_new_states == True)
        Loop: iteration = 1 to 7:
            a. System-2 generates proposal:
               {
                 "states": [{"key": string, "type": string, "initial_value": any, "description": string}],
                 "usage_instructions": string
               }
            b. System-1 evaluates proposal:
               Output: approval_score (0.0 .. 1.0), guidance (string)
            c. If approval_score >= 0.90 OR iteration == 7:
                 Persist states to State Store;
                 BREAK;
               Else:
                 Pass guidance as feedback for next iteration.

Step 3: JEV Prompt Conflict & Novelty Analysis
        Input: Baseline Prompt, Active Dynamic Prompt, Admin Directive
        Output:
          - has_contradiction (boolean)
          - brings_new_info (boolean)
          - merge_strategy ("override_conflicts_and_add_new" | "pure_addition" | "redundant")

Step 4: System-2 Prompt Synthesis & Merge
        Input: Baseline Prompt, Active Prompt, Directive, JEV Analysis, Approved States
        Output: Merged Dynamic Prompt
        Action: Persist Merged Prompt into State/Prompt Store.

Step 5: Admin Acknowledgment
        Output: Structural confirmation message containing iteration count, JEV approval score, and updated keys.
```

---

### NODE 2: State Store & Persistence Layer
Abstract key-value and relational schema required for platform execution:

#### Schema A: `bot_state` (Configuration & Dynamic Metrics)
* `key` (String, Primary Key): Identifier of variable.
* `value` (String / Serialized JSON): Current value.
* `updated_at` (Timestamp): Last modification time.

**Standard Keys**:
* `custom_system_prompt`: Active compiled dynamic instructions for System-2.
* `last_processed_timestamp`: High-water mark timestamp for message deduplication.
* `lives_<participant>`: Integer counters representing remaining lives.
* `offset_<participant>`: Integer adjustments to challenge formula target quotas.

#### Schema B: `progress` (Daily Challenge Ledger)
* `date` (String, ISO-8601 `YYYY-MM-DD`, Primary Key)
* `<participant>_done` (Integer / Boolean, default 0): Completion status for each user.
* `updated_at` (Timestamp)

#### Schema C: `chat_history` (Conversation Audit Log)
* `id` (Auto-increment integer, Primary Key)
* `chat_id` (Identifier)
* `user_name` (String)
* `role` (String: "user", "coach", "system")
* `text` (Text)
* `created_at` (Timestamp)

---

### NODE 3: Regular Runtime & Persona Generation
* **Input**: Unprocessed conversation history, current date/time, trigger type (User Push vs Scheduled Timer).
* **Execution Flow**:
  1. **State Assembly**:
     - Compute daily challenge targets: `base_formula(day_index) + state["offset_<user>"]`.
     - Fetch completion status for today from `progress`.
     - Fetch active live metrics (`lives_<user>`) from `bot_state`.
  2. **System Instruction Compilation**:
     - Baseline Immutable Prompt (`SYSTEM_PROMPT_TEMPLATE`)
     - Injected Live Metrics block (`❤️ АКТУАЛЬНІ ЖИТТЯ У БД`)
     - Active Dynamic Override block (`custom_system_prompt`)
  3. **Cognitive Call with Tools**:
     - Invoke System-2 passing chat context and executable Tool definitions.
     - Execute any triggered tool calls (e.g. marking progress, modifying targets).
     - Persist assistant response and advance `last_processed_timestamp`.

---

## 4. DOMAIN TOOLS SPECIFICATION

1. `mark_participant_done(participant_id: string, is_done: boolean) -> string`
   - Sets completion flag in `progress` for current date.
2. `check_challenge_status() -> string`
   - Returns daily targets, completion flags, and live life counters for all participants.
3. `get_challenge_rules() -> string`
   - Returns immutable base rules of the fitness ladder challenge.
4. `admin_set_lives(secret_phrase: string, participant_lives: map) -> string`
   - Mutates `lives_<participant>` keys in `bot_state`.
5. `admin_set_pushup_targets(secret_phrase: string, target_overrides: map) -> string`
   - Computes required delta offsets and updates `offset_<participant>` in `bot_state`.
6. `get_random_anime_quote(category: string) -> string`
   - Retrieves motivation/refusal/praise quote corresponding to user state.

---

## 5. BEHAVIORAL INVARIANTS & RESPONSE PATTERNS

Regardless of implementation language, the client MUST enforce these response patterns:

1. **Micro-Paragraph Structure**:
   - Every response must consist of short paragraphs (2–3 lines maximum).
   - Paragraphs must be separated by an explicit double line break (`\n\n`).
2. **Visual Hierarchy & Markdown**:
   - Names, quotas, and statuses must use bold formatting (`**текст**`).
   - Sarcasm, inner thoughts, and side-remarks must use italics (`*текст*`).
   - Thematic emojis must initiate sections (e.g., 🦾, 🔥, ⚡, 👑, ❤️, 📊).
3. **Strict Coach Persona ("Спорт Зануда")**:
   - Uncompromising humorous fitness coach.
   - Refusal of unauthorized complaints or excuses without valid secret passphrase.
4. **Mandatory Titles**:
   - If dynamic prompt dictates a title (e.g., `Сонцеликий Льоша`), using the bare name without title is strictly disallowed.
5. **State Accounting Discipline**:
   - Unmet quotas past deadline decrement the participant's life counter.
   - Zero lives triggers failure / forfeit protocol.