---
name: fix-searchapi-time-params
overview: Implement date range parsing from search queries and pass them as `time_period_min` and `time_period_max` parameters to the SearchAPI.io provider to ensure accurate time-restricted search results.
todos:
  - id: add-imports
    content: Add `from dateutil import parser` to `数据pipeline/数据链接信息搜刮.py`
    status: completed
  - id: implement-extraction
    content: Add `_extract_date_params` method to `SearchApiIoProvider` class in `数据pipeline/数据链接信息搜刮.py`
    status: completed
    dependencies:
      - add-imports
  - id: update-search-method
    content: Update `SearchApiIoProvider.search` method to use extracted date params in `数据pipeline/数据链接信息搜刮.py`
    status: completed
    dependencies:
      - implement-extraction
---

## Product Overview

Fix the `SearchAPI.io` integration to correctly parse and apply date range restrictions from search queries.

## Core Features

- **Date Range Parsing**: Extract date patterns (e.g., `after:YYYY-MM-DD`, `before:YYYY-MM-DD`, `YYYY-MM-DD..YYYY-MM-DD`) from the user's search query string.
- **API Parameter Mapping**: Map extracted dates to `SearchAPI.io`'s `time_period_min` and `time_period_max` parameters.
- **Query Cleaning**: Remove the raw date strings from the query to ensure a clean search term is sent to the API.

## Tech Stack

- **Language**: Python
- **Libraries**: `python-dateutil` (for robust date parsing), `re` (for pattern matching)

## Implementation Details

### Modified Files

`数据pipeline/数据链接信息搜刮.py`:

- **Import**: Add `from dateutil import parser`
- **Class**: `SearchApiIoProvider`
    - **New Method**: `_extract_date_params(self, query: str)`
        - Uses regex to find `after:`, `before:`, and `..` range patterns.
        - Parses found dates into `MM/DD/YYYY` format (SearchAPI preferred).
        - Returns cleaned query and date params.
    - **Modified Method**: `search(...)`
        - Calls `_extract_date_params`.
        - Adds `time_period_min` and `time_period_max` to the API request parameters if they exist.

### Key Algorithms

**Date Extraction Logic**:

1.  Scan for `after:(\S+)` and `before:(\S+)`.
2.  Scan for `(\d{4}-\d{1,2}-\d{1,2})\.\.(\d{4}-\d{1,2}-\d{1,2})` (Google range syntax).
3.  Use `dateutil.parser.parse` to convert strings to date objects.
4.  Format dates as strings (e.g., `YYYY-MM-DD` or `MM/DD/YYYY`).
5.  Strip matched patterns from the original query string.