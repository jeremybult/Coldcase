diff --git a/README.md b/README.md
index 1ba1c866565819ccb425dfb95285e08690db5871..6fd9e45fbef58c34df622722254811cc8048c4e6 100644
--- a/README.md
+++ b/README.md
@@ -1 +1,31 @@
-# Coldcase
\ No newline at end of file
+# Coldcase
+
+Single-file FastAPI MVP for missing persons (MP) and unidentified human remains (UHR) matching with optional external search hooks.
+
+## Quickstart
+
+```bash
+pip install fastapi uvicorn sqlalchemy psycopg[binary] pydantic requests pgvector
+export DATABASE_URL="postgresql+psycopg://fcix:fcix@localhost:5432/fcix"
+uvicorn FCIX_ONEFILE_PLATFORM:app --reload
+```
+
+## External search configuration
+
+External connectors are opt-in and require API bases/keys from authorized providers.
+
+```bash
+# Optional, for approved/authorized APIs only
+export GEDMATCH_API_BASE="https://example-gedmatch-api"
+export FTDNA_API_BASE="https://example-ftdna-api"
+export DNA_JUSTICE_API_BASE="https://example-dna-justice-api"
+
+# Optional web search enrichment
+export WEB_SEARCH_PROVIDER="bing"  # or other provider you wrap
+export WEB_SEARCH_API_BASE="https://example-search-api"
+export WEB_SEARCH_API_KEY="your-key"
+```
+
+.
