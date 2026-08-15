Project: Fedora-Based Personal AI Witness System

Project Concept
- Build a serious Fedora-based personal AI witness system, not a toy prototype.
- Its purpose is to capture and process the meaningful text I read, write, and see on my screen, store that as structured long-term memory, and reflect patterns, contradictions, themes, and possible insights back to me over time.
- This is not a therapist, not a diagnosis engine, and not an omniscient judge.
- It should act like a witness with memory: observing, remembering, retrieving, and reflecting with restraint.

Core Purpose
- Capture what I read, write, and attend to on my computer.
- Convert that stream into structured memory.
- Build an evidence-based long-term picture of my recurring interests, beliefs, tensions, habits, and shifts.
- Answer reflective questions grounded in accumulated memory, not just recent context.

My Environment
- I use Fedora Linux.
- I am not a traditional programmer, but I build software with AI assistance.
- I already run a local AI on a llama server on my PC.
- I already use Cloudflare Workers into Supabase for RAG and memory.
- This project should be designed to fit into that stack from the beginning.

Design Philosophy
- Build the system correctly from the ground up.
- Do not design it as a disposable MVP that will later be patched into seriousness.
- Start with a proper architecture, strong schemas, clear data boundaries, modular pipelines, and long-term maintainability.
- Think house, not shack.
- That means:
  - full memory model first
  - proper ingestion pipeline first
  - durable storage and retrieval model first
  - explicit epistemic rules first
  - flexible interfaces later

What the System Should Capture
- I want it to capture text from what appears on screen, including:
  - articles I read
  - things I write
  - conversations
  - any meaningful text displayed to me
- The system should aim to capture the textual substance of my screen activity, not just app names or window titles.
- This may require:
  - accessibility APIs where possible
  - browser integration where possible
  - OCR and/or screenshot-to-text pipelines where necessary
  - focused capture of visible screen regions or active windows
- The system should treat screen-visible text as a first-class input source, not an afterthought.

Architectural Premise
- The system should be designed from the beginning as a multi-layer observatory with six major layers:
  - ingestion
  - extraction
  - normalization
  - memory
  - retrieval
  - reflection

Layer 1: Ingestion
- Collect raw activity from the machine in near real time.
- Sources may include:
  - active window data
  - browser tab and page data
  - visible screen content
  - clipboard events if enabled
  - user-authored documents
  - chat logs
  - notes and journals
- Screen-visible text capture must be a core feature of the system.
- Prefer direct text extraction where possible.
- Fall back to OCR or screenshot analysis only where necessary.

Layer 2: Extraction
- Turn raw screen or app activity into usable text records.
- Separate extraction by source type:
  - browser text extraction
  - app/document text extraction
  - chat/conversation extraction
  - screen OCR extraction
- Every extracted record should include:
  - timestamp
  - source application
  - window or document title
  - raw extracted text
  - text length
  - capture method
  - confidence score
  - session identifier

Layer 3: Normalization
- Convert extracted text into clean, structured memory-ready units.
- Tasks include:
  - deduplication
  - chunking
  - source tagging
  - entity/topic extraction
  - session grouping
  - relevance scoring
  - compression into summaries when needed
- The system should avoid storing identical repeated captures endlessly.
- It should preserve important text while controlling redundancy.

Layer 4: Memory
- The system must maintain durable long-term memory in both local structured files and Supabase-backed retrieval storage.
- Memory should be separated into types:
  - raw captures
  - normalized records
  - summaries
  - observations
  - rolling themes
  - reflections
  - embeddings and metadata
- Local file memory should be readable and inspectable by me.
- Supabase memory should support retrieval, search, and semantic reflection.

Layer 5: Retrieval
- Before answering any reflective question, the system must retrieve relevant evidence from memory.
- Retrieval should combine:
  - semantic similarity
  - time range
  - topic tags
  - source type
  - theme relevance
- Reflection must be memory-grounded, not improvised from a blank prompt.

Layer 6: Reflection
- The AI’s role is to reflect patterns, tensions, and possible meanings back to me with care and restraint.
- It should distinguish between:
  - observed facts
  - recurring patterns
  - inferred tendencies
  - contradictions
  - open questions
  - tentative synthesis
- It should not diagnose.
- It should not make grand pronouncements with weak evidence.
- It should speak as a witness, not as a judge.

Core Outputs
- The system should generate:
  - raw text capture records
  - daily session summaries
  - structured observation notes
  - rolling theme files
  - weekly and monthly reflections
  - on-demand answers to reflective questions

Memory Structure
- /memory/raw/
  - raw text captures and screen-derived records
- /memory/normalized/
  - cleaned and deduplicated records
- /memory/daily/
  - daily summaries and session rollups
- /memory/observations/
  - short structured notes about recurring patterns and notable tensions
- /memory/themes/
  - long-lived topic files such as God, work, identity, love, fear, creativity, worldview
- /memory/reflections/
  - weekly and monthly synthesis
- /memory/system/
  - prompts, schemas, rules, config
- /memory/index/
  - retrieval metadata and helper files

Schema Requirements
- Every memory object should be structured, not just loose prose.
- Recommended common fields:
  - id
  - timestamp
  - source_type
  - source_app
  - title
  - session_id
  - tags
  - summary
  - observed_facts
  - inferred_tendencies
  - contradictions
  - open_questions
  - confidence
  - evidence_refs
  - embedding_status

Use of Existing Stack
- Use the local llama server for:
  - first-pass summarization
  - chunk classification
  - topic tagging
  - observation drafting
- Use Cloudflare Workers for:
  - secure orchestration
  - routing to remote services if needed
  - bridging between local and Supabase workflows
- Use Supabase for:
  - embeddings
  - vector retrieval
  - metadata tables
  - long-term searchable memory
- Optionally use a stronger remote model for:
  - higher-order synthesis
  - long-horizon reflection
  - better cross-theme interpretation

Screen Text Capture Requirement
- This is a first-class requirement.
- The system should be designed from the beginning to handle screen-visible text intelligently.
- Claude Code should help architect a proper screen-text ingestion path, not tack one on later.
- The design should support multiple capture methods depending on context:
  - direct DOM/browser extraction when possible
  - app/document text extraction when possible
  - OCR or screenshot-to-text when needed
- The architecture should assume that visible text capture is part of the foundation.

Data Quality Rules
- Do not flood memory with junk.
- Deduplicate repeated screen content.
- Segment text by session and context.
- Preserve source traceability.
- Store enough raw evidence to support later reflection.
- Compress aggressively only after preserving recoverable source references.

Behavior Rules for the AI
- Do not diagnose me.
- Do not declare me insane, broken, or disordered.
- Do not confuse repetition with truth.
- Do not inflate weak patterns into metaphysical conclusions.
- Clearly separate evidence from interpretation.
- Use careful, restrained language.
- Ground claims in the memory archive.

Questions the System Should Eventually Answer Well
- What have I spent the most attention on over the last 90 days?
- What themes recur across what I read, write, and discuss?
- What beliefs appear stable?
- What contradictions recur in my language?
- What unresolved questions dominate my archive?
- How has my thinking changed over time?
- What truths do I seem to be circling without resolving?

Non-Goals
- A toy dashboard
- Productivity gimmicks
- Diagnosis
- Fake pseudo-spiritual certainty
- An omniscient surveillance deity persona
- A rushed prototype whose architecture has to be ripped apart later

Implementation Request to Claude Code
- Help me design this as a serious system from the ground up.
- Start with:
  - full architecture
  - ingestion model
  - screen text capture strategy for Fedora
  - durable folder structure
  - schemas for all memory layers
  - Supabase tables and retrieval model
  - reflection pipeline
  - orchestration between local llama server, Cloudflare Workers, and Supabase
- I do not want a throwaway toy MVP architecture.
- I want a strong foundation and a sane load-bearing structure from the start.

Paste-Ready Prompt for Claude Code
I want to build a serious Fedora-based personal AI witness system, not a toy prototype. Its purpose is to capture and process the meaningful text I read, write, and see on my screen, store that as structured long-term memory, and reflect patterns, contradictions, themes, and possible insights back to me over time. I already run a local AI on a llama server on my PC, and I already use Cloudflare Workers into Supabase for RAG and memory. This system should be designed correctly from the ground up, with a solid architecture, durable schemas, proper ingestion and retrieval layers, and clear epistemic boundaries. Screen-visible text capture is a first-class requirement, including articles, conversations, and things I write. Please help me design the full architecture first: ingestion, screen text extraction strategy for Fedora, normalization, memory model, local folder structure, Supabase schema, retrieval pipeline, reflection pipeline, and orchestration across local and remote components. The system should act like a witness with memory, not a therapist or judge. It must distinguish observed facts from interpretation and avoid diagnosis or fake certainty.
