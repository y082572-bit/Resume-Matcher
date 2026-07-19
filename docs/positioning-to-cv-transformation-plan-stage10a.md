# Positioning-to-CV Transformation Plan - Stage 10A

## Cel

Etap 10A dodaje deterministyczny plan pośredni między raportem Career Positioning a istniejącym generatorem dopasowanego CV. Plan opisuje decyzje, ale nie generuje ani nie zapisuje nowej treści CV.

## Problem biznesowy

Dotychczasowy przepływ tailorowania przekazuje aktualne Resume, Job, słowa kluczowe i plan kompetencji bezpośrednio do generatora różnic opartego na LLM. Brakowało jawnego, audytowalnego kontraktu określającego priorytety, ograniczenia i twierdzenia zabronione przed generowaniem.

## Punkt startowy

Implementacja bazuje na lokalnym `main` o HEAD `2698293f3cf01d8067f08ab2744506f07e415473` i jest prowadzona na branchu `feature/positioning-to-cv-plan-stage10a`, bez commita i bez publikowania zmian.

## Single Sources of Truth

Plan korzysta wyłącznie z:

- wpisów Truth Library dopuszczonych przez reguły auto-CV;
- aktualnego, ustrukturyzowanego Resume;
- aktualnego Job powiązanego z tym Resume;
- deterministycznego Career Positioning Report;
- jawnych reguł transformacji Etapu 10A.

Statusy `NIEJASNE`, `USUNIĘTE` i `PRAWDA_Z_DOKUMENTU_DO_ZATWIERDZENIA`, wpisy wymagające akceptacji, wyłączone z CV, zablokowane albo spoza listy auto-CV są bezwarunkowo ignorowane.

## Architektura

Istniejący punkt generowania znajduje się w preview tailorowania Resume. Dla danych ustrukturyzowanych uruchamia plan celów kompetencyjnych, generator różnic, nakładanie i weryfikację zmian; dla danych nieustrukturyzowanych używa starszego generatora pełnej odpowiedzi. Confirm waliduje hash preview, tworzy potomny Resume i Improvement, a warstwa bazy emituje metryki utworzenia i wygenerowania Resume.

Nowy serwis jest funkcją czystą. Router Career Positioning pobiera Job i Resume, sprawdza ich relację, ładuje Truth Library, buduje istniejący Career Positioning Report i przekazuje wszystkie cztery źródła do planera. Docelowym punktem integracji po akceptacji planu jest wywołanie go przed `generate_skill_target_plan` i `generate_resume_diffs`. Nie powstaje drugi generator.

Truth Index i Truth Validator zostały objęte audytem. Obecny tailor nie wywołuje ich bezpośrednio; Etap 10A ponownie wykorzystuje wspólną normalizację Truth, filtr dopuszczenia wpisów i zasadę scope zatrudnienia. Pełna walidacja wygenerowanych twierdzeń należy do Etapu 10D.

## Model danych

Publiczny `CVTransformationPlan` 1.1 zawiera wersję planu, identyfikatory Resume i Job, czas wygenerowania, poziomy Career Positioning, strategię, flagę ręcznej kontroli, pięć sekcji instrukcji, `evidence_permissions`, uniwersalne zakazy, ograniczenia i zagregowane podsumowanie źródeł.

Każda instrukcja zawiera akcję, sekcję, strukturalny identyfikator źródła, krótkie `display_label`, kod i opis powodu, siłę dowodu oraz flagę ręcznej kontroli. `source_reference` ma postać `resume:section:ordinal` albo `positioning:section:ordinal`; ordinal jest nadawany po kanonicznym sortowaniu. Referencja nie jest skrótem treści ani identyfikatorem Truth.

`EvidencePermission` zawiera `claim_code`, referencję konkretnego istniejącego elementu, siłę dowodu, dozwoloną operację i flagę ręcznej kontroli. Dozwolone są wyłącznie `KEEP_EXISTING`, `EMPHASIZE_EXISTING` i `REPHRASE_EXISTING`. Etap 10A nie posiada `GENERATE_NEW`.

## Akcje transformacji

- `KEEP` — zachowanie znaczenia i priorytetu.
- `EMPHASIZE` — silniejsze wyeksponowanie prawdziwego, istotnego elementu.
- `DEEMPHASIZE` — obniżenie priorytetu bez usuwania prawdziwego faktu.
- `REPHRASE` — zamiar zmiany narracji bez zmiany znaczenia.
- `OMIT` — zarezerwowane dla treści nieistotnych, powtarzalnych lub technicznie zbędnych; serwis nie używa go do ukrywania niewygodnych faktów.
- `HUMAN_REVIEW` — konflikt, brak dowodu, niejasność albo materialna zmiana profilu.

## Mapowanie strategii Career Positioning

| Strategia | Instrukcja dla podsumowania | Kontrola |
| --- | --- | --- |
| `REVIEW_REQUIRED` | `HUMAN_REVIEW` | zawsze |
| `JUNIOR_ENTRY` | `KEEP` | według raportu |
| `SPECIALIST_DELIVERY` | `EMPHASIZE` | według raportu |
| `EXPERT_AUTHORITY` | `EMPHASIZE` | według raportu |
| `MANAGER_LEADERSHIP` | `REPHRASE` | oddzielne dowody menedżerskie |
| `DIRECTOR_SCALE` | `REPHRASE` | oddzielne sygnały dyrektorskie |
| `EXECUTIVE_ENTERPRISE` | `HUMAN_REVIEW` | zawsze |
| `CONTROLLED_FLATTENING` | `REPHRASE` | zachowanie faktów i tytułów |
| `CONTROLLED_ELEVATION` | `HUMAN_REVIEW` | zawsze |
| `BALANCED` | `KEEP` | według raportu |

## Expert

Plan eksponuje bezpośrednie i transferowalne kompetencje oraz zatwierdzone wyniki. Przy `CONTROLLED_FLATTENING` osłabia dominację kontekstu dyrektorskiego lub strategicznego, ale nie zmienia tytułu, nie udaje braku doświadczenia i nie usuwa osiągnięć liczbowych.

## Manager

Narracja menedżerska jest wyłącznie zamiarem przeformułowania. People management, wielkość zespołu i zarządzanie menedżerami są niezależnymi kategoriami dowodowymi. Koordynacja nie jest automatycznie traktowana jako formalne zarządzanie ludźmi. Każdy plan nadal zawiera wszystkie guardraile, a zatwierdzony fakt może utworzyć tylko permission dla konkretnego elementu.

## Director

Strategia, budżet, P&L, skala organizacyjna, zarządzanie menedżerami oraz relacje Board/C-level są oceniane oddzielnie. Dowód jednego sygnału nie usuwa zakazu dla innego sygnału.

## Prohibited Claims

Kontrakt zawsze zwraca dokładnie 10 kodów: `PNL_WITHOUT_EVIDENCE`, `BUDGET_WITHOUT_EVIDENCE`, `BOARD_WITHOUT_EVIDENCE`, `PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE`, `MANAGING_MANAGERS_WITHOUT_EVIDENCE`, `TEAM_SIZE_WITHOUT_EVIDENCE`, `TECHNICAL_SKILL_WITHOUT_EVIDENCE`, `LANGUAGE_LEVEL_WITHOUT_EVIDENCE`, `CERTIFICATION_WITHOUT_EVIDENCE` i `QUANTIFIED_RESULT_WITHOUT_EVIDENCE`.

Są to uniwersalne reguły „nie twórz bez dowodu”, a nie opis globalnego braku dowodu. Obecność dowodu nie usuwa żadnego guardraila. Zatwierdzony Python daje permission wyłącznie konkretnemu elementowi Python; nie odblokowuje SQL, AWS ani Java. Dowód budżetu, P&L, Board, people management, managing managers, team size, strategii i skali organizacyjnej jest klasyfikowany niezależnie.

Wynik liczbowy otrzymuje permission tylko przy dokładnej zgodności znormalizowanego twierdzenia oraz zgodnym pracodawcy i zgodnej roli lub okresie. Substring, inna wartość, inna firma albo status niezaufany prowadzą do `HUMAN_REVIEW`.

## Prywatność

Response model ma `extra="forbid"`. DTO nie zawiera identyfikatorów wpisów Truth, metadanych bazy, tokenów lifecycle, kluczy operacji, korelacji, ścieżek lokalnych ani pełnej Truth Library. `display_label` pochodzi wyłącznie z aktualnego Resume albo publicznego Career Positioning Report, jest jednowierszowe i ograniczone do 120 znaków. Nie pochodzi z surowej Truth Library.

## Deterministyczność

Kanoniczna serializacja, normalizacja tekstu, zbiory i jawne sortowanie zapewniają semantycznie identyczny wynik niezależnie od kolejności doświadczeń, kompetencji, narzędzi, osiągnięć i wpisów Truth. Po sortowaniu nadawane są strukturalne ordinale. Jedynym polem zależnym od czasu jest `generated_at`.

## Read-only

Endpoint wykonuje wyłącznie odczyty `get_job` i `get_resume`. Nie aktualizuje Job, Resume ani Application, nie tworzy Resume lub Improvement, nie zapisuje planu i nie emituje `MetricEvent`.

## Brak użycia LLM

Planer nie importuje ani nie wywołuje LiteLLM, Ollama, OpenAI-compatible API, generatora różnic ani żadnej funkcji generującej tekst. Decyzje są rezultatem jawnych reguł i raportu Career Positioning.

## Kontrakt API

`GET /api/v1/jobs/{job_id}/resumes/{resume_id}/transformation-plan`

- `200` — poprawny plan;
- `404 JOB_NOT_FOUND` — brak Job;
- `404 RESUME_NOT_FOUND` — brak Resume;
- `422 INCONSISTENT_JOB_RESUME_RELATION` — Job nie wskazuje żądanego źródłowego Resume;
- `422 RESUME_NOT_READY` — Resume nie jest gotowe lub nie ma danych ustrukturyzowanych;
- zachowane są istniejące błędy Truth Library (`404`, `400`, `422`, `500`).

## Testy

Testy jednostkowe obejmują komplet strategii, wszystkie guardraile, cross-category false positives, scope osiągnięć, EvidencePermission, strukturalne referencje, enumy, filtrowanie danych niezaufanych, prywatność, deterministyczność oraz brak LLM. Testy integracyjne używają publicznego API FastAPI i `isolated_db`, kontrolując Resume, Job, Application, metryki i brak ekspozycji Truth. Frontend ma ścisły parser kontraktu 1.1 oraz klienta GET z obsługą kodów błędów i `AbortSignal`.

## Ograniczenia

Etap 10A nie generuje docelowego tekstu, nie porównuje proponowanych zdań z Truth Validator i nie przechowuje decyzji użytkownika. Dopasowanie ogólnej istotności doświadczeń do Job opiera się na deterministycznym pokryciu tokenów. Zatwierdzona Truth nigdy nie usuwa prohibited claim; tworzy wyłącznie permission dla istniejącego elementu.

## Ryzyka

Ścisłe wzorce mogą nie rozpoznać nietypowego synonimu odpowiedzialności; bezpiecznym skutkiem jest brak permission, nie globalne odblokowanie. Dodanie elementu wcześniejszego w sortowaniu może zmienić późniejsze ordinale, dlatego klient powinien traktować referencje jako identyfikatory konkretnej wersji preview. Generator musi traktować prohibited claims jako ograniczenia twarde, a `HUMAN_REVIEW` jako blokadę do czasu akceptacji.

## Etap 10B

Etap 10B dodaje UI przeglądu i akceptacji planu. Nie generuje jeszcze nowego tekstu.

## Etap 10C

Etap 10C generuje treść przez LLM wyłącznie według zaakceptowanego planu, scoped permissions i uniwersalnych guardraili.

## Etap 10D

Etap 10D uruchamia Truth Validator, przedstawia diff i obsługuje akceptację wygenerowanych zmian.

## Etap 10E

Etap 10E zapisuje zaakceptowaną wersję Resume oraz obsługuje eksport DOCX/PDF.

## Definition of Done

- kompletny, wersjonowany response model Pydantic i zgodne typy TypeScript;
- deterministyczny serwis z kompletnym mapowaniem istniejących strategii;
- publiczny endpoint GET z kontrolą relacji Job–Resume;
- brak LLM, mutacji, zapisu planu i metryk;
- co najmniej 30 testów jednostkowych, 15 integracyjnych i 12 frontendowych;
- zielone testy dedykowane i pełne, typecheck, lint oraz build;
- kontrola prywatności, deterministyczności, zakresu i pakiet audytowy poza repozytorium.
