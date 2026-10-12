# **Multi-Agent Financial Advisor for Pakistan’s Retail Investors** 

**Final Year Project Proposal** 

**Session FALL 2026** 

BS in CS/SE/AI/CY 




## Department of CS/AI/SE/CY 

## FAST School of Computing 

FAST National University, Karachi Campus 

18th September 2026 

#### **Project Registration** 

|Project ID (for office use)|||
|---|---|---|
|Type of project|[  ] Traditional              [  ] Industrial|[X] Continuing|
|Nature of project|[ ] Development            [X] Research & Deve|lopment    [  ]   Research|
|Sustainable Development<br>Goals(SDGs)|[  ] Good Health and Well-Being<br>[X]  Industry, Innovation, and Infrastructure<br>[X] Decent Work and Economic Growth|[  ] Quality Education<br>[  ] Gender Equality<br>[  ]  Climate Action|
||[X] Artificial Intelligence (AI)   [  ] Data Scie<br>[  ] Internet of Things (IoT)       [  ] Blockcha|nce and Analytics<br>in|
|Area of specialization|[  ] Mobile App Development   [  ]  Web Dev<br>[  ] Game Development             [X]  Natural<br>[  ] Other|elopment               [  ] Cybersecurity<br>Language Processing (NLP)|



|||**P**|**roject Gro**|**up Members**|||
|---|---|---|---|---|---|---|
|Sr.#|Reg. #|Student Name|CGPA|Email ID|Phone #|Signature|
|(i)|23K-3031|Falah Hilal (Group<br>Leader)|2.97|k233031@nu.edu.pk|+92-330-326-18<br>75|Falah.|
|(ii)|23K-3023|Mahrukh Shariq|3.26|k233023@nu.edu.pk|+92-317-287-61<br>86|Mahrukh.|
|(iii)|23K-0505|Maryam Asif|3.61|k230505@nu.edu.pk|+92-335-286-35<br>84|Maryam.|



**Declaration:** FYP group members have cleared all prerequisite courses For FYP-I  as per their degree requirements. 

### **1. Project Abstract** 

Multi-agent large language model (LLM) systems have recently been applied to financial decision-making, using specialised agents and structured debate to generate trading signals (Trading Agents, Xiao et al., 2024/25) and, separately, personalised portfolio allocation from an individual’s risk profile (Li, Gu & Ding, 2025). However, no reviewed work combines multi-agent debate, individual risk personalisation, and portfolio-level output in one system, and no academically evaluated multi-agent financial advisory system exists for the Pakistan Stock Exchange (PSX) or any emerging market. Whether structured debate actually improves outcomes over simpler methods such as majority voting is itself an unresolved, contested question in the current literature (Choi, Zhu & Li, NeurIPS 2025 vs. Hu, arXiv:2510.12697), and the hallucination-mitigation and explainability benefits claimed for such systems are asserted more often than they are measured. 

2 of 14 

This project proposes a five-agent architecture (Macro Agent, Sector Analyst Agent, Risk Profiler Agent, Debate Facilitator Agent, and Portfolio Constructor) with a structured debate protocol and a source-grounded citation-enforcement layer for Pakistani retail investors. Agents retrieve from PSX equity data, State Bank of Pakistan monetary policy documents, World Bank indicators, and Fin BERT-classified financial news, and cannot make a quantitative claim without a retrievable source citation. A risk profiling questionnaire produces a hard constraints object that bounds the final allocation, and every recommendation is accompanied by a plain-English audit trail. The system is evaluated through a controlled single-agent versus multi-agent ablation measuring hallucination rate and explanation quality (the specific evaluation TradingAgents itself never ran), supported by back testing on verified Pakistani market windows, an explainability survey with 20 non-finance participants, and a personalisation check across four investor profiles. 

### **2. Introduction** 

##### **2.1 Research Domain** 

This project operates at the intersection of multi-agent Large Language Model (LLM) systems, financial technology, and explainable artificial intelligence. The research domain concerns the application of structured adversarial debate between specialised AI agents to the problem of personalised retail investment advisory, specifically within the context of Pakistan’s emerging capital market. 

##### **2.2 Why This Domain Is Important** 

Financial advisory shapes some of the most consequential decisions an individual can make, yet access to quality guidance remains deeply unequal. In Pakistan, the financial inclusion rate has grown substantially in recent years (primarily measuring basic bank account access rather than investment advisory specifically). The Pakistan Stock Exchange (PSX) has comparatively few active retail accounts for a country of over 240 million people. Sophisticated, personalised portfolio guidance remains predominantly available to high-net-worth individuals through private banking relationships, while the majority of salaried professionals, small business owners, and young savers have no reliable, intelligent, or explainable source of investment advice. 

This gap has direct economic consequences. Retail investors who lack advisory access either leave savings idle, losing real value during inflationary periods, or rely on unqualified sources that expose them to uninformed risk. 

##### **2.3 Current Situation** 

Existing solutions for retail investors in Pakistan fall into two inadequate categories. Basic brokerage applications provide raw market data with no analytical guidance. Rule-based tools apply uniform allocation formulas to all users regardless of individual risk profile, financial goals, time horizon, or current macroeconomic conditions. While at least one Pakistani product (PSX Invest) markets itself as using AI and LLM-generated explanations, no academically rigorous, multi-agent, peer-evaluated advisory system exists for Pakistani retail investors or for any emerging market. No existing tool, academic or commercial, combines multi-agent debate architecture, source-grounded citation enforcement, structured risk-profiling-to-portfolio construction, and a regulatory-disclosure-grade audit trail. 

3 of 14 

##### **2.4 Why AI and Computational Research Is Relevant** 

Recent advances in large language models have demonstrated that AI systems can reason about complex, multi-dimensional financial problems in natural language. Multi-agent LLM frameworks, where specialised agents with distinct roles collaborate and debate, have been shown to outperform rule-based approaches on financial decision tasks in narrow settings. The closest prior work, TradingAgents (Xiao, Sun, Luo & Wang, arXiv:2412.20138, 2024/25), demonstrated that role-specialised multi-agent financial reasoning can outperform rule-based baselines on US equities, but tested only single-ticker institutional trading, never measured hallucination reduction or explainability despite claiming both, and left personalisation and portfolio-level construction entirely unaddressed. Whether structured debate specifically outperforms simpler aggregation methods remains an actively contested empirical question in the literature (Choi, Zhu & Li, NeurIPS 2025; Hu, arXiv:2510.12697; Okawa, ICML 2026), which is precisely what makes it a genuine research question rather than a foregone conclusion. 

### **3. Problem Statement** 

Three distinct gaps characterise the current state of investment advisory for Pakistan’s retail investors, none of which existing solutions address simultaneously. 

###### **_Gap 1: No academically rigorous, personalised, multi-agent advisory system for PSX retail investors_** 

No peer-reviewed, evaluated multi-agent financial advisory system exists for the Pakistan Stock Exchange or any emerging market. Existing academic work on PSX/KSE-100 is confined to price-prediction and volatility-forecasting using classical ML approaches, none of which involve multi-agent design, portfolio construction, risk profiling, or explainability evaluation. Industry products that claim AI features are not SECP-licensed, not multi-agent, and carry no published evaluation. The gap is specifically the absence of an academically evaluated, personalised, multi-agent system. 

###### **_Gap 2: The personalisation and portfolio-construction gap in multi-agent financial AI_** 

The closest work combining personalised risk profiling with LLM-based portfolio allocation (Li, Gu & Ding, arXiv:2512.12922, 2025) is a single sequential pipeline with no debate, no arbitration, and no retrieval-grounded citation enforcement. The closest work combining multi-agent debate with portfolio-level construction (Luo et al., arXiv:2501.00826, 2025) has no individual risk profiling. No reviewed work combines all three: multi-agent debate, individual personalisation, and portfolio-level output. 

###### **_Gap 3: Hallucination-rate and explainability claims are asserted but not evaluated_** 

TradingAgents claims hallucination reduction and improved explainability as benefits of its multi-agent debate design but reports zero evidence for either. Whether structured debate actually reduces hallucination versus a single-agent baseline, and whether debate-derived explanations are more comprehensible to non-expert users than standard outputs, are specific empirical questions that remain unanswered in the literature. This project’s evaluation is designed around measuring exactly these two outcomes: the evaluation TradingAgents never ran. 

###### **_Research Questions_** 

4 of 14 

RQ1: Does the multi-agent debate architecture reduce the hallucination rate compared to a single-agent LLM baseline on identical Pakistani market inputs? 

RQ2: Does structured, adversarial debate produce more comprehensible, higher-quality explanations than a single-agent baseline and a multi-agent voting baseline? 

RQ3: Does the system generate personalised portfolio allocations that vary meaningfully across investor risk profiles, time horizons, and financial goals? 

### **4. Motivation** 

The motivation behind this project is to make personalised, explainable investment reasoning accessible to Pakistani retail investors who currently lack it, and to rigorously evaluate whether multi-agent debate design actually delivers the hallucination-reduction and explainability benefits that existing systems claim without measuring. 

Specifically, this project aims to: 

- Provide personalised, intelligent portfolio guidance to Pakistani retail investors who cannot access private financial advisory 

- Replace uniform allocation formulas with agent-based reasoning that accounts for each user’s specific risk tolerance, investment horizon, financial goals, and existing holdings 

- Produce a plain-English audit trail that makes the reasoning behind a recommendation readable to a non-technical retail user and traceable for regulatory purposes 

- ● Conduct the controlled hallucination-rate and explainability ablation that TradingAgents asserts but never measures (a genuine methodological contribution independent of financial performance) 

- Apply and evaluate multi-agent debate within Pakistan’s specific market context (PSX data, SBP monetary policy signals, PKR macroeconomic dynamics) where no such evaluation has been conducted 

##### **4.1 Aims and Objectives** 

The primary aim is to design, implement, and evaluate a multi-agent LLM financial advisory system that generates personalised, explainable, and empirically validated investment recommendations for Pakistan’s retail investors, and to measure whether the multi-agent debate mechanism reduces hallucination and improves explanation quality compared to a single-agent baseline. 

Specific objectives: 

- Design a five-agent architecture (Macro Agent, Sector Analyst Agent, Risk Profiler Agent, Debate Facilitator Agent, and Portfolio Constructor) with clearly specified roles, data sources, and inter-agent communication protocols 

- Implement a source-grounded RAG pipeline in which agents cannot make quantitative claims without a retrievable source citation 

- Implement a structured three-round debate protocol with a deterministic conservative-default tiebreaker, where the Debate Facilitator Agent arbitrates based on retrieved evidence and risk constraint compliance 

- Produce a plain-English audit trail for every recommendation (evaluated against Flesch-Kincaid readability targets and through a user comprehension survey) 

5 of 14 

- Evaluate through: (a) a controlled hallucination-rate and explanation-quality ablation (the primary contribution); (b) back testing on verified Pakistani market windows; (c) an explainability survey with 20 non-finance participants; (d) a personalisation checks across four investor profiles 

##### **4.2 Scope of the Project** 

###### **In scope:** 

- Five-agent system: Macro Agent, Sector Analyst Agent, Risk Profiler Agent, Debate Facilitator Agent, and Portfolio Constructor 

- Source-grounded RAG pipeline (LlamaIndex + ChromaDB, Fin BERT embeddings) with claim-blocking enforcement 

- PSX equity data (psxdata), SBP PDFs (pdfplumber), World Bank API, Yahoo Finance .KA fallback, NewsAPI 

- User onboarding and risk profiling questionnaire stored as structured JSON 

- ● Plain-English audit trail generation and display 

- Controlled ablation comparing four decision-making configurations on identical inputs: 

   - the full multi-agent debate system, 

   - a single monolithic LLM given the same data and prompts, 

   - a multi-agent signal-averaging/voting baseline with no debate, and a deterministic fixed-rule allocation (static 60/40) 

- All three LLM-based configurations run on the same fixed backbone LLM (GPT-4o-mini) so that architecture, not model capability, is the variable under test; the tiered production models described in Sections 5 and 6 (Claude Sonnet / Claude Haiku) apply to the deployed prototype only, not to the ablation 

- Backtesting on four historical Pakistani market windows with walk-forward validation 

- Hallucination detection applied identically across the three LLM-based configurations: the automated citation-matching checker flags any unmatched claim in each configuration's output. The fixed-rule allocation baseline produces no LLM output, so it is scored on back test metrics only 

- Explainability survey (20 participants) and Flesch-Kincaid readability scoring 

- ● NextJS frontend, FastAPI + Django backend, LangChain orchestration, LangSmith observability 

###### **Out of scope:** 

- Live trade execution: recommendations only 

- Urdu language sentiment analysis: English-only, documented as a limitation 

- Real-time intraday data: daily bars sufficient 

- SECP licensing: framed as educational portfolio analysis 

### **5. Proposed Methodology** 

The system is built in five sequential phases, each with a definition of done before the next begins. The primary research contribution is the controlled ablation measuring hallucination rate and explanation quality; this drives the evaluation design. A claim, defined as any numerical or factual statement in an agent’s output, is counted as a hallucination if an automated checker cannot match it to a supporting passage in the retrieved source documents; 

6 of 14 

the hallucination rate is the proportion of flagged claims per response, and a random 15% subsample of flagged and unflagged claims is manually annotated by the two team members to verify the automated checker’s precision and recall. 

Two mechanisms enforce this grounding requirement. A retrieved factual claim, such as a macroeconomic indicator, a sector figure, or a filing value, must cite a passage in the retrieved source documents; the automated checker flags any unmatched claim. A derived numerical value, such as a portfolio weight, the Sharpe ratio, or another risk metric, is produced by a deterministic computation module rather than generated directly by an LLM, so no agent states a calculated figure the module has not actually returned. 

##### **5.1 System Flow** 

- **Step 1 (User Onboarding):** Structured questionnaire captures risk tolerance, investment time horizon, financial goal, existing holdings, and investable capital. Stored as a JSON profile. 

- **Step 2 (Risk Profiler Agent):** Processes the onboarding JSON and generates a constraints object (max equity allocation, minimum liquidity requirement, time-horizon classification, behavioural flags) before any market analysis begins. This is a hard boundary on the Portfolio Constructor, not a soft preference. The risk profile is isolated from the Macro and Sector agents to prevent it from biasing market signal analysis. 

- **Step 3 (Parallel Data Retrieval):** Macro Agent and Sector Analyst Agent retrieve independently and simultaneously. Macro Agent: SBP PDFs, World Bank API, FRED, news feeds. Sector Analyst: psxdata library, Yahoo Finance .KA fallback, PSX company filings, FinBERT-classified news headlines. Every quantitative claim must cite a retrieved source document; agents cannot proceed without grounding. 

- **Step 4 (Structured Debate):** Unless both agents produce perfectly identical outputs, which in practice will almost never occur, the debate activates. Agents state their positions with retrieved evidence. The debate runs for a maximum of three rounds. In each round, each agent responds specifically to the other’s claims, not by restating its own position. The Debate Facilitator manages the debate and arbitrates based on the quality of retrieved evidence and whether each agent’s recommendation respects the Risk Profiler’s constraints. 

- **Step 5 (Deterministic Tiebreaker)** : In case of non-consensus after three rounds, the Debate Facilitator Agent uses a deterministic rule to choose the path that best satisfies the Risk Profiler’s constraints, defaulting to the more conservative allocation. The decision is written as the verdict of the Debate Facilitator Agent and passed to the Portfolio Constructor, which combines it with the constraints of the Risk Profiler to produce the final allocation and audit trail. 

- **Step 6 (Output):** The Portfolio Constructor outputs a final allocation JSON file, and a simple English audit trail that captures the reasoning steps, evidence citation, and the debate exchange with agent attribution. Allocation weights and all derived metrics (returns, volatility, Sharpe ratio) are computed by a deterministic Python layer rather than by the LLM, which only writes the reasoning. 

- **Step 7 (Display On Front End):** The NextJS frontend displays the final allocation, a collapsible audit trail, and the live agent trace. 

7 of 14 

##### **5.1 Functional Requirements** 

|**ID**|**Requirement**|**Priority**|
|---|---|---|
|FR-01|System shall onboard users via a structured questionnaire capturing risk profile,<br>time horizon, goals,and existingholdings|High|
|FR-02|Risk Profiler Agent shall generate a constraints JSON before market analysis<br>begins;constraints are hard boundaries|High|
|FR-03|Risk profile JSON shall be isolated from Macro and Sector agents, not visible<br>duringmarket signal retrieval|High|
|FR-04|Macro Agent shall retrieve and reason about SBP policy rate, inflation, GDP, FX,<br>and news sentiment from sourced documents|High|
|FR-05|Sector Analyst Agent shall retrieve and reason about PSX sector performance, PE<br>ratios,earnings,and FinBERT-classified news|High|
|FR-06|Every quantitative claim shall be blocked if no retrieved source document supports<br>it|High|
|FR-07|Debate Facilitator Agent shall manage a maximum three-round debate and arbitrate<br>based on retrieved evidence and risk constraint compliance, applying a deterministic<br>conservative-default tie-breaker if no consensus is reached|High|
|FR-08|Portfolio Constructor shall synthesize the debate outcome and Risk Profiler<br>constraints into a finalportfolio allocation and audit trail|High|
|FR-09|System shallproduce a finalportfolio allocation JSON with asset class weights|High|
|FR-10|System shall produce a plain-English audit trail logging every debate step with<br>agent attribution and source citations|High|
|FR-11|Full recommendation cycle shall complete within an empirically-validated latency<br>target (working target: 45 seconds on standard hardware), confirmed during<br>implementation rather than assumed apriori|Medium|
|FR-12|Audit trail shall target Flesch-Kincaid readability equivalent to an 8th–10th grade<br>readinglevel|Medium|
|FR-13|Portfolio Constructor shall compute final allocation weights and risk metrics,<br>including the Sharpe ratio, through a deterministic code module; no agent-generated<br>value maysubstitute for the module’s output|High|



##### **5.2 Non-Functional Requirements** 

|**ID**|**Requirement**|**Category**|
|---|---|---|
|NFR-01|Full recommendation cycle under a working target of 45 seconds on standard<br>hardware; mitigations if exceeded include parallelising Macro/Sector retrieval,<br>usingcost-tiered models throughout,and cachingrepeated retrievalqueries|Performance|
|NFR-02|LangSmith traces all LLM calls and tool uses: full replaycapability|Observability|
|NFR-03|All agent outputs logged with timestamps and model versions|Reproducibility|
|NFR-04|No quantitative claim without a retrievable source citation: hard architectural<br>constraint|Accuracy|
|NFR-05|API costper cycle within budget via tiered model routing|Cost|
|NFR-06|Frontend accessible on desktopand mobile browsers|Accessibility|
|NFR-07|Audit trail immutable, timestamped, and agent-attributed: structured for<br>regulatorytraceability|Compliance|
|NFR-08|Every derived numerical value (portfolio weights, Sharpe ratio, other risk<br>metrics) is produced by deterministic code, never generated by an LLM: hard<br>architectural constraint|Accuracy|



### **6. System Architecture** 

The system follows a three-tier architecture: a presentation layer (NextJS frontend), an application layer (FastAPI + Django backend, LangChain orchestration, LangSmith 

8 of 14 

observability, RAG pipeline), and a data layer (PSX and macro data sources, user profile store). 

The overall workflow is: 

User → Onboarding Form → Risk Profiler Agent → Macro and Sector Agents (parallel, RAG-grounded) → Structured Debate → Portfolio Constructor → Allocation + Audit Trail → Frontend Display 

##### **6.1 Architecture Layers** 

|**Layer**|**Components**|**Technology**|
|---|---|---|
|Presentation|User onboarding form, allocation display, live agent trace<br>viewer,collapsible audit trail|NextJS, TailwindCSS|
|Application|FastAPI REST backend, Django admin layer, LangChain<br>agent<br>orchestration,<br>LangSmith<br>observability,<br>LlamaIndex + ChromaDB RAG pipeline, FinBERT<br>sentiment classifier|FastAPI,<br>Django,<br>LangChain,<br>LangSmith, LlamaIndex, ChromaDB,<br>FinBERT|
|Data|PSX equity data, SBP PDFs, World Bank API, Yahoo<br>Finance fallback, NewsAPI, FRED, PostgreSQL for user<br>profiles|psxdata,<br>pdfplumber,<br>wbgapi,<br>yfinance,<br>NewsAPI,<br>FRED,<br>PostgreSQL|
|LLM|Claude Sonnet for Portfolio Constructor (production);<br>Claude Haiku / GPT-4o-mini for Macro and Sector agents<br>(cost-tiered). The controlled ablation instead fixes<br>GPT-4o-mini across every agent so that architecture is the<br>onlyvariable under test.|Anthropic API, OpenAI API|
|Observability|Traces everyLLM call,tool use,and agent interaction|LangSmith|



##### **6.2 Agent Design** 

|**Agent**|**Role**|**Data Sources**|**Output Schema**|
|---|---|---|---|
|Risk Profiler Agent|Processes<br>onboarding<br>JSON;<br>generates constraint profile before<br>any market analysis begins; result<br>isolated from market agents|User onboarding JSON<br>only|risk_level,<br>max_equity_pct,<br>min_liquidity_pct,<br>time_horizon,<br>behavioral_flags|
|Macro Agent|Analyses Pakistan macroeconomic<br>environment;<br>every<br>quantitative<br>claim must cite a retrieved source|SBP PDFs, World Bank<br>API, FRED,|macro_stance<br>(hawkish/neutral/dovish),<br>key_signals[],<br>confidence_score,<br>source_citations[]|
|Sector<br>Analyst<br>Agent|Evaluates<br>sector-level<br>signals;<br>FinBERT classifies news headlines;<br>every claim grounded|psxdata, Yahoo Finance<br>.KA,<br>PSX<br>company<br>filings,<br>FinBERT<br>pipeline,NewsAPI|sector_recommendations[],<br>sentiment_summary,<br>source_citations[]|
|Debate<br>Facilitator<br>Agent|Manages debate; arbitrates based on<br>retrieved<br>evidence<br>and<br>risk<br>constraints;|Output of Macro Agent<br>and Sector Agent|debate_verdict,<br>winning_agent,<br>confidence_score,<br>tie_breaker_triggered(bool)|
|Portfolio<br>Constructor|applies tiebreaker; computes final<br>allocation weights and risk metrics<br>through<br>a<br>deterministic<br>code<br>module; produces audit trail|Outputs of all three<br>agents: no new external<br>data|final_allocation{},<br>audit_trail[],<br>debate_summary{}|



##### **6.3 Debate Protocol** 

The debate is held under a three-round protocol, controlled by the Debate Facilitator Agent. In the case of a tie, the tie is broken deterministically: 

9 of 14 

- **Round 1** : Macro Agent and Sector Analyst Agent announce initial positions with supporting evidence. The Debate Facilitator Agent is assigned Risk Profiler constraints as hard boundaries. 

- **Round 2:** Each agent takes on the specific claims of the other; they cannot just reiterate their own position. The Debate Facilitator Agent keeps track of both arguments. 

- **Round 3** : All agents submit a final position with a new confidence score. The Debate Facilitator Agent begins arbitration. 

- **Tie-breaker:** If no consensus can be reached after three rounds, the Debate Facilitator Agent chooses the path that best satisfies Risk Profiler constraints, defaulting to the more conservative allocation, and records this decision as its verdict. 

- **Handoff:** The debate_verdict, winning_agent, confidence_score, tie_breaker_triggered decisions of the Debate Facilitator Agent is handed to the Portfolio Constructor which combines it with the constraints of the Risk Profiler to generate the final allocation and audit trail 

##### **6.4 Tech Stack** 

|**Layer**|**Technology**|**Purpose**|
|---|---|---|
|Frontend|NextJS + TailwindCSS|Onboarding, allocation display, live agent trace, audit<br>trail viewer|
|Backend|FastAPI + Django|REST API, request handling, user management, session<br>storage|
|Agent Orchestration|LangChain|Agent definition, tool binding, debate protocol, RAG<br>integration|
|Observability|LangSmith|Full tracingof everyLLM call,tool use,and agent step|
|RAG Pipeline|LlamaIndex + ChromaDB|Document ingestion, chunking, FinBERT embeddings,<br>semantic retrieval|
|Embeddings|FinBERT (ProsusAI/finbert)|Finance-domain semantic search and news sentiment<br>classification|
|LLM: Production|Claude Sonnet|Portfolio Constructor: high-quality reasoning and audit<br>trailgeneration|
|LLM: Development|Claude Haiku / GPT-4o-mini|Cost-efficient Macro and Sector data-retrieval agents;<br>fixed as the sole backbone across all agents in the<br>controlled ablation|
|PSX Data|psxdata<br>(PyPI)<br>+<br>Yahoo<br>Finance .KA|OHLCV, sector summaries, fundamentals: psxdata<br>primary,Yahoo Finance fallback|
|Macro Data|SBP PDFs (pdfplumber) +<br>World Bank API(wbgapi)|SBP for current rates; World Bank for historical trends|
|Sentiment|FinBERT<br>pipeline<br>(HuggingFace)|Bulk classification of English-language financial news<br>headlines|
|Database|PostgreSQL|Userprofiles,session data,audit trail storage|



### **7. Individual Tasks** 

The table below lists individual responsibilities and the tentative schedule for each team member. 

|**Team Member**|**Activity**|**Tentative Date**|
|---|---|---|
|**Falah Hilal (23K-3031)**|Literature review: multi-agent frameworks, debate mechanisms,<br>verified citation matrix|Sep 2026|



10 of 14 

|**Team Member**|**Activity**|**Tentative Date**|
|---|---|---|
||Data pipeline: psxdata, Yahoo Finance fallback, SBP PDF<br>parsing,World Bank API,FRED,NewsAPI|Sep–Oct 2026|
||RAG pipeline: LlamaIndex + ChromaDB setup, FinBERT<br>embeddings,ingestionpipeline,claim-blockingenforcement|Oct–Nov 2026|
||Macro Agent: system prompt, RAG retrieval, output schema,<br>source-citation validation|Jan 2027|
||Sector<br>Analyst<br>Agent:<br>FinBERT<br>news<br>classification,<br>fundamentalspipeline,output schema|Jan–Feb 2027|
||Backtest framework: Sharpe ratio, drawdown, walk-forward<br>validation scripting|Apr 2027|
||Thesis chapters 2(Literature Review)and 4(Data)|May2027|
|**Mahrukh**<br>**Shariq**<br>**(23K-3023)**|Literature<br>review:<br>explainability,<br>hallucination,<br>and<br>personalisation sections|Sep 2026|
||Risk profiling research: questionnaire design, investor profile<br>taxonomy,constraint schema design|Oct 2026|
||Agent role specification: system prompts, output schemas,<br>inter-agent contract definition|Nov 2026|
||Risk Profiler Agent: implementation, profile storage, constraints<br>JSON,isolation from market agents|Dec 2026|
||Debate Facilitator Agent: Manages debate; arbitrates based on<br>retrieved evidence and risk constraints;|Jan–Feb 2027|
||Portfolio Constructor: applies tiebreaker; produces allocation<br>and audit trail|Feb 2027|
||LangChain debate protocol: three-round structure, adversarial<br>forcing,kill-switch|Feb 2027|
||Audit trail design andplain-English serialisation|Mar 2027|
||User explainability survey: design, recruitment (20 participants),<br>execution,Flesch-Kincaid scoring|Apr 2027|
||Thesis chapters 3(Methodology)and 5(Evaluation)|May2027|
|**Maryam Asif (23K-0505)**|Literature review: evaluation methodology, benchmarks, prior<br>system comparison|Sep 2026|
||Environment setup: repository structure, CI, PostgreSQL<br>schema design,API contract definition|Sep–Oct 2026|
||FastAPI<br>+ Django backend: REST endpoints, session<br>management,PostgreSQL integration|Nov–Dec 2026|
||NextJS frontend: onboarding form, allocation display, live agent<br>trace,audit trail viewer|Feb–Mar 2027|
||LangSmith integration: observability, cost tracking, full call<br>tracing|Mar–Apr 2027|
||Ablation study: debate system vs. single-agent baseline vs.<br>voting baseline vs. fixed-rule baseline; hallucination rate;<br>explanationqualityscoring|Mar–Apr 2027|
||Personalisation test: four investor profiles, output differentiation<br>verification|Apr–May 2027|
||Demopreparation, poster,and final integration testing|May2027|



### **8. Gantt Chart** 

The chart below maps the individual tasks above onto the two-phase FYP-I / FYP-II schedule, showing which activities run in parallel and which run in series. Each cell represents one calendar month; a task’s shaded cells are the months it is active in. 

11 of 14 

|**Task**<br>**FYP-I  —  Sep 2026 to Dec 2**|**Sep**<br>**026**|**Oct**|**Nov**|**Dec**|**Jan**|**Feb**|**Mar**|**Apr **|**May**|
|---|---|---|---|---|---|---|---|---|---|
|Literature Review||||||||||
|Environment & Schema Setup||||||||||
|Data Pipeline (PSX, SBP, WB,<br>FRED)||||||||||
|Risk Profiling Research &<br>Design||||||||||
|RAG<br>Pipeline<br>+<br>Claim<br>Blocking||||||||||
|Agent Role Specification||||||||||
|FastAPI + Django Backend||||||||||
|Risk Profiler Agent||||||||||
|**FYP-II  —  Jan 2027 to May**|**2027**|||||||||
|Macro Agent||||||||||
|Sector Analyst Agent||||||||||
|Debate Facilitator Agent||||||||||
|Portfolio Constructor||||||||||
|NextJS Frontend||||||||||
|LangChain Debate Protocol||||||||||
|Audit Trail Serialisation||||||||||
|LangSmith Observability||||||||||
|Ablation Study||||||||||
|Backtest<br>+<br>Walk-Forward<br>Validation||||||||||
|ExplainabilitySurvey||||||||||
|Personalisation Test||||||||||
|Demo Preparation & Poster||||||||||
|Thesis Writing||||||||||



### **9. Tools and Technologies** 

|**Tool / Technology**|**Version / Source**|**Purpose**|
|---|---|---|
|NextJS|Latest stable|Frontend: onboarding, allocation display, audit trail<br>viewer|
|FastAPI|Latest stable|PrimaryREST API backend|
|Django|Latest stable|Admin and management layer,database ORM|
|LangChain|Latest stable|Agent orchestration, tool binding, debate protocol, RAG<br>integration|
|LangSmith|Cloud service|Full trace of everyLLM call and agent step|
|LlamaIndex|Latest stable|Document ingestion,chunking,RAG retrievalpipeline|
|ChromaDB|Latest stable|Vector store for embedded financial documents|
|FinBERT (ProsusAI/finbert)|HuggingFace|Finance-domain<br>embeddings<br>and<br>news<br>sentiment<br>classification|
|Claude Sonnet|Anthropic API|Portfolio Constructor:production-qualityreasoning|
|Claude Haiku / GPT-4o-mini|Anthropic / OpenAI API|Cost-tiered LLMs for Macro and Sector data-retrieval<br>agents; fixed as the sole backbone across all agents in the<br>controlled ablation|
|psxdata|PyPI|PSX equity data: OHLCV, sector summaries, KSE-100<br>constituents|
|yfinance|PyPI|Yahoo Finance fallback for Pakistani stocks(.KA suffix)|
|wbgapi|PyPI|World Bank API Python wrapper: Pakistan macro<br>indicators|
|pdfplumber|PyPI|PDF parsing and table extraction: SBP and PSX filing<br>documents|



12 of 14 

|**Tool / Technology**|**Version / Source**|**Purpose**|
|---|---|---|
|PostgreSQL|Latest stable|Userprofiles,session data,audit trail logs|
|GitHub|github.com|Version control and collaboration|
|Postman|Latest stable|API testingand documentation|



### **10. Expected Deliverables** 

- A functional multi-agent LLM financial advisory web application: NextJS frontend, FastAPI + Django backend, LangChain orchestration with LangSmith observability 

- Five specialised AI agents (Macro, Sector Analyst, Risk Profiler, Debate Facilitator, Portfolio Constructor) with a structured three-round debate protocol and deterministic tiebreaker 

- Source-grounded RAG pipeline with claim-blocking enforcement: agents cannot make quantitative claims without a retrieved source citation 

- Plain-English audit trail for every recommendation: readable by retail users and traceable for regulatory purposes 

- Controlled ablation results across all four configurations (full multi-agent debate, single-agent LLM, multi-agent voting, and fixed-rule allocation) on identical inputs, measuring hallucination rate on the three LLM-based configurations and explanation quality (the primary methodological contribution) 

- Backtesting results across Pakistani market windows with walk-forward validation 

- ● Explainability user survey results (20 participants, Flesch-Kincaid readability scores) 

- Personalisation verification across four investor profiles 

- FYP-I Report 

- FYP-II Thesis, seven chapters: Introduction, Literature Review, Methodology, Data, Evaluation, Discussion, Conclusion 

### **11. References** 

[1] Y. Xiao, E. Sun, D. Luo, and W. Wang, “TradingAgents: Multi-Agents LLM Financial Trading Framework,” arXiv:2412.20138, 2024/25. Available: https://arxiv.org/abs/2412.20138 

- [2] Y. Yu, Z. Yao, H. Li et al., “FinCon: A Synthesized LLM Multi-Agent System with Conceptual Verbal Reinforcement for Enhanced Financial Decision Making,” arXiv:2407.06567, NeurIPS 2024. Available: https://arxiv.org/abs/2407.06567 

[3] Li, Gu, and Ding, “LLM-based Personalized Portfolio Recommender,” arXiv:2512.12922, 2025. Available: https://arxiv.org/abs/2512.12922 

- [4] Choi, Zhu, and Li, “Debate or Vote: Which Yields Better Decisions in Multi-Agent LLMs?” arXiv:2508.17536, NeurIPS 2025 Spotlight. Available: https://arxiv.org/abs/2508.17536 

[5] Hu, “Multi-Agent Debate for LLM Judges with Adaptive Stability Detection,” arXiv:2510.12697, 2025. Available: https://arxiv.org/abs/2510.12697 

[6] Okawa, “Emergence of Biased Consensus in Multi-Agent LLM Debates,” arXiv:2608.02827, ICML 2026. Available: https://arxiv.org/abs/2608.02827 

13 of 14 

[7] Guo, Wu, and Yiu, “FinGround: Detecting and Grounding Financial Hallucinations via Atomic Claim Verification,” arXiv:2604.23588, 2026. Available: https://arxiv.org/abs/2604.23588 

[8] Y. Du et al., “Improving Factuality and Reasoning in Language Models through Multiagent Debate,” arXiv:2305.14325, ICML 2024. Available: https://arxiv.org/abs/2305.14325 [9] Luo, Feng, Xu, Tasca, and Liu, “LLM-Powered Multi-Agent System for Automated Crypto Portfolio Management,” arXiv:2501.00826, 2025. Available: https://arxiv.org/abs/2501.00826 

[10] Ioannou et al., “The Role of Explainability in AI-Driven Financial Decision Making,” Information Systems Frontiers, 2026. Available: https://doi.org/10.1007/s10796-026-10727-1 

[11] Ji et al., “PHANTOM: A Benchmark for Hallucination Detection in Financial Long-Context QA,” NeurIPS 2025. Available: https://neurips.cc/virtual/2025/loc/san-diego/poster/121830 

[12] Motger, Oriol, Marco, and Franch, “Multi-Agent Debate Strategies: Survey, Taxonomy, and Challenges,” arXiv:2607.26212. Available: https://arxiv.org/abs/2607.26212 

[13] Saha et al. (BlackRock), “LLM Agents for Investment Management,” ACM ICAIF’25. Available: https://doi.org/10.1145/3768292.3770387 

[14] D. Araci, “FinBERT: Financial Sentiment Analysis with Pre-trained Language Models,” arXiv:1908.10063, 2019. 

[15] S. Yao et al., “ReAct: Synergizing Reasoning and Acting in Language Models,” arXiv:2210.03629, 2023. 

[16] T. Miller, “Explanation in Artificial Intelligence: Insights from the Social Sciences,” Artificial Intelligence, vol. 267, pp. 1–38, 2019. 

[17] F. Doshi-Velez and B. Kim, “Towards a Rigorous Science of Interpretable Machine Learning,” arXiv:1702.08608, 2017. 

[18] M. Wooldridge, An Introduction to MultiAgent Systems, 2nd ed. Wiley, 2009. 

[19] G. Irving, P. Christiano, and D. Amodei, “AI Safety via Debate,” arXiv:1805.00899, 2018. 

14 of 14 

