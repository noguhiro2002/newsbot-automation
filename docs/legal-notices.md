# Legal Notices

Newsbot Automation source code is licensed under the [MIT License](../LICENSE).
That license does not grant rights to third-party service data, trademarks, or
brand assets.

## NCBI and PubMed

Newsbot Automation can retrieve PubMed records through the NCBI E-utilities.
Users and operators must review and comply with the
[NCBI Disclaimer and Copyright Notice](https://www.ncbi.nlm.nih.gov/home/about/policies/)
and the
[E-utilities Usage Guidelines and Requirements](https://www.ncbi.nlm.nih.gov/books/NBK25497/).

PubMed abstracts can contain material protected by United States and foreign
copyright laws. Permission for reproduction, redistribution, or commercial use
beyond applicable legal exceptions must be obtained from the relevant copyright
holder. NCBI, NLM, and NIH do not endorse Newsbot Automation.

Operators must register the `tool` and developer `email` values used by this
software with NCBI and must respect the applicable request-rate limits.

## Crossref

Crossref metadata is retrieved through the Crossref REST API. Operators must
follow the
[Crossref REST API etiquette and rate limits](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/).
Crossref does not endorse Newsbot Automation.

## ChemRxiv

ChemRxiv metadata is retrieved exclusively through Crossref `posted-content`.
The client uses the Crossref public or polite pool policy, serializes requests,
caches responses, honors `Retry-After`, and backs off on HTTP 429 and transient
server errors. Operators must continue to follow Crossref's current terms and
published API guidance. ChemRxiv and Wiley do not endorse Newsbot Automation.

## Trademarks

Discord and the Discord logo are trademarks of Discord Inc. Python and the
Python logos are trademarks or registered trademarks of the Python Software
Foundation. OpenAI and Codex are trademarks of OpenAI. All marks are used only
to identify compatible services or technologies. This project is not affiliated
with or endorsed by Discord Inc., the Python Software Foundation, OpenAI, NCBI,
NLM, NIH, or Crossref.
