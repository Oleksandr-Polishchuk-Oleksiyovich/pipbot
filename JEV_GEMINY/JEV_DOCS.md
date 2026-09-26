Jev API
POST compact business fields. The server turns them into Jev state + questions and returns typed choices, probabilities, and scores. Jev does not replace the agent’s main model, execute tools, browse the web, or write long-form reasoning.

Base URL and auth
All calls are POST to https://www.jevai.org. Do not use http or the apex host. Off-site clients send Authorization: Bearer with a personal key from /agent/keys. Browser calls on this site may use the login session instead.

curl -sS https://www.jevai.org/api/v1/decisions/tool-guard \
  -H "Authorization: Bearer $JEV_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tool":"issue_customer_refund","action":"Refund USD 680 after a disputed duplicate charge","arguments_summary":["order_id=ord_7429","amount_usd=680"],"side_effects":["Moves funds","Changes the order payment state"],"safeguards":["Customer identity and duplicate charge verified"],"policy":["Refunds above USD 500 require human approval"],"reversibility":"partially_reversible"}'
Body size is capped at 32 KiB. Responses are { code, message, data }. code 0 is success. Treat probabilities as signals, not authorization.

{
  "code": 0,
  "message": "ok",
  "data": {
    "decision": "confirm",
    "confidence": 0.86,
    "probabilities": { "allow": 0.08, "confirm": 0.71, "review": 0.16, "deny": 0.05 },
    "guidance": "Obtain explicit user confirmation before invoking the tool.",
    "guidance_source": "jev_preset",
    "answers": {}
  }
}
Endpoints
Six workflows. Five are presets: you send business fields, the server attaches Jev questions. The sixth is native Decisions when no preset fits. MCP tools use the same fields.

When	REST	MCP
Allow, confirm, review, or deny a consequential tool call. Jev does not execute the tool.	/api/v1/decisions/tool-guard	jev_guard_tool_call
Choose among models the environment can actually invoke.	/api/v1/decisions/model-route	jev_route_model
Pick proceed_fast, deep_review, split_task, or block for an ambiguous or risky task.	/api/v1/decisions/route	jev_route_task
Judge whether supplied evidence supports one claim. Jev does not browse.	/api/v1/decisions/research	jev_check_research
Check whether an objective is actually complete before reporting it done.	/api/v1/decisions/completion	jev_review_completion
No preset fits. Send your own state plus choice, noul, or score questions.	/api/v1/decisions	jev_decide
jev_guard_tool_call
Allow, confirm, review, or deny a consequential tool call. Jev does not execute the tool.

POST /api/v1/decisions/tool-guard. Required: tool, action. Optional: arguments_summary, side_effects, safeguards, policy, reversibility. Typical decision: allow | confirm | review | deny.

{
  "tool": "issue_customer_refund",
  "action": "Refund USD 680 after a disputed duplicate charge",
  "arguments_summary": [
    "order_id=ord_7429",
    "amount_usd=680"
  ],
  "side_effects": [
    "Moves funds",
    "Changes the order payment state"
  ],
  "safeguards": [
    "Customer identity and duplicate charge verified"
  ],
  "policy": [
    "Refunds above USD 500 require human approval"
  ],
  "reversibility": "partially_reversible"
}
jev_route_model
Choose among models the environment can actually invoke.

POST /api/v1/decisions/model-route. Required: task, candidates (at least two, each with id and description). Optional: priorities, constraints, stakes. Typical decision: the selected candidate id.

{
  "task": "Review a complex customer dispute with 100k context and tool use",
  "candidates": [
    {
      "id": "fast-model",
      "description": "Fast general model with 32k context",
      "cost": "low",
      "latency": "low"
    },
    {
      "id": "reasoning-model",
      "description": "Strong reasoning, 200k context and tool use",
      "cost": "high",
      "latency": "medium"
    }
  ],
  "priorities": [
    "quality",
    "context",
    "tool_use",
    "cost"
  ],
  "constraints": [
    "Customer data must remain within approved tools"
  ],
  "stakes": "high"
}
jev_route_task
Pick proceed_fast, deep_review, split_task, or block for an ambiguous or risky task.

POST /api/v1/decisions/route. Required: task. Optional: evidence, constraints. Typical decision: proceed_fast | deep_review | split_task | block.

{
  "task": "Resolve a request involving account access and a disputed charge",
  "evidence": [
    "Customer identity is verified",
    "Refund eligibility is unclear"
  ],
  "constraints": [
    "Do not change account state without confirmation"
  ]
}
jev_check_research
Judge whether supplied evidence supports one claim. Jev does not browse.

POST /api/v1/decisions/research. Required: claim. Optional: evidence, source_quality, stakes. Typical decision: accept | verify_more | reject.

{
  "claim": "This order qualifies for an expedited refund under the policy",
  "evidence": [
    "Order arrived three days late",
    "Policy requires five days"
  ],
  "source_quality": "mixed",
  "stakes": "medium"
}
jev_review_completion
Check whether an objective is actually complete before reporting it done.

POST /api/v1/decisions/completion. Required: objective. Optional: completed_work, verification, known_gaps. Typical decision: complete | verify_more | incomplete.

{
  "objective": "Resolve a customer's duplicate-charge report",
  "completed_work": [
    "Matched both charges to the same order"
  ],
  "verification": [
    "Customer identity and charge records verified"
  ],
  "known_gaps": [
    "Refund has not been approved or issued"
  ]
}
jev_decide
No preset fits. Send your own state plus choice, noul, or score questions.

POST /api/v1/decisions. Required: state, questions. Optional: model (Jev identifier only). Typical decision: per question in answers.

{
  "state": {
    "customer_message": "I was charged twice for order ord_7429.",
    "duplicate_charge_usd": 680,
    "customer_identity_verified": true,
    "policy": "Refunds above USD 500 require human approval."
  },
  "questions": {
    "action": {
      "type": "choice",
      "instructions": "Choose the safest next action.",
      "criteria": {
        "allow": "Issue the refund immediately.",
        "review": "Require human approval before issuing the refund.",
        "deny": "Reject the refund request."
      }
    },
    "needs_human_review": {
      "type": "noul",
      "instructions": "Does this refund require human review under the stated policy?"
    },
    "risk": {
      "type": "score",
      "instructions": "Score the financial and policy risk.",
      "criteria": [
        "Low",
        "Moderate",
        "High",
        "Critical"
      ]
    }
  }
}
Native questions
POST /api/v1/decisions accepts model, state, and questions. Only Jev identifiers are allowed, such as typesafe-ai/jev. Do not call /chat/completions. Each question is choice, score, or noul.

choice: pick one option from criteria. Returns choice, probabilities, confidence.
score: ordered levels. Returns score, probabilities, optional legend.
noul: probability of yes. Returns noul between 0 and 1.
Keep irreversible actions behind your own human approval. Do not send passwords, API keys, or unrelated private data.