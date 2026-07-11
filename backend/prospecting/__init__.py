"""Prospecting workcell — zipcode-driven local-business discovery + callsheet.

Pipeline (in-process, CLI-driven for v1):

    zipcodes + industries
      -> place_search.discover_for_zipcode (Google Places New textSearch)
      -> scorer.score_prospect / classify_priority
      -> callsheet.build_call_sheet (templated; LLM-driven in a later phase)
      -> csv_export.write_call_list (32-column daily call_list_*.csv)

Run via CLI:

    python -m backend.prospecting.run_daily \\
        --zipcodes 95993,95991 --industries finance,roofing

The HTTP/SQS path lands when app.py + worker.py are added in a follow-up phase.
"""
