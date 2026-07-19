# Transformation Plan Review UI - Stage 10B

## Cel

Etap 10B udostępnia użytkownikowi odczyt deterministycznego planu z Etapu 10A,
jawne decyzje `ACCEPT`, `REJECT` i `REQUEST_REVIEW`, wspólne potwierdzenie
guardraili oraz bezpieczny zapis decyzji dla przyszłego Etapu 10C. Nie generuje
ani nie modyfikuje treści CV.

## Punkt startowy

Implementację rozpoczęto z czystego `main` na
`79589c00acd294e2251455c153f75b2ffe0b3d30`, zgodnego z `origin/main`.

## Architektura

Architecture-first audit wykazał, że `Improvement` przechowuje wynik procesu
tailoringu i nie reprezentuje akceptacji planu. `Application` jest kartą
trackera, a `Resume` i `Job` są źródłami planu. Dlatego review otrzymało osobną
trasę `/resumes/{resume_id}/positioning/plan`, a persistencja osobny minimalny
model `CVTransformationPlanApproval`.

Backend każdorazowo regeneruje aktualny plan przez ten sam deterministyczny
service. Frontend wyłącznie wyświetla kontrakt i wysyła decyzje; nie interpretuje
Truth Library, nie zmienia akcji i nie konstruuje permissions.

## Plan Fingerprint

`plan_fingerprint` jest backendowym SHA-256 kanonicznego publicznego planu po
usunięciu `generated_at` i samego fingerprintu. Payload obejmuje wersję,
identyfikatory, strategię, akcje, permissions, guardraile, ograniczenia i
publiczne podsumowanie źródeł. Dodatkowe, nieujawniane SHA-256 kanonicznych
rewizji Resume, Job i Truth Library wiążą approval ze stanem źródeł. Kolejność
semantycznie nieistotnych kolekcji jest normalizowana. Surowe dane źródłowe nie
są zwracane ani przechowywane w approval.

## Model decyzji

`TransformationPlanDecision` zawiera wyłącznie:

- `source_reference`;
- `decision`: `ACCEPT`, `REJECT` albo `REQUEST_REVIEW`.

`ACCEPT` akceptuje dokładnie rekomendowaną akcję. `REJECT` wyłącza element z
przyszłego użycia bez zastępstwa. `REQUEST_REVIEW` pozostawia go
nierozstrzygniętym. Request nie przyjmuje action, reason, permission ani claims.

## Model zatwierdzenia

`CVTransformationPlanApproval` przechowuje identyfikator approval, wersję i
fingerprint planu, identyfikatory Resume i Job, status, posortowane decyzje,
potwierdzenie guardraili oraz daty utworzenia i aktualizacji. Nie przechowuje CV,
oferty, Career Positioning ani Truth Library.

## Statusy

- `DRAFT`: jawnie zapisana, również niekompletna wersja robocza;
- `APPROVED`: komplet decyzji bez `REQUEST_REVIEW` i potwierdzone guardraile;
- `REQUIRES_REVIEW`: komplet decyzji zawiera `REQUEST_REVIEW`;
- `SUPERSEDED`: zapis dotyczy innego fingerprintu niż bieżący plan.

Publiczny DTO waliduje również semantykę statusu po obu stronach kontraktu.
`APPROVED` wymaga potwierdzonych guardraili i nie dopuszcza `REQUEST_REVIEW`.
`REQUIRES_REVIEW` wymaga potwierdzonych guardraili oraz co najmniej jednej
decyzji `REQUEST_REVIEW`. Kompletność względem aktualnego planu pozostaje
odpowiedzialnością serwisu.

## Idempotencja

Unikalny klucz `(job_id, resume_id, plan_fingerprint)` i atomowy SQLite
`INSERT ... ON CONFLICT DO NOTHING` gwarantują jeden approval dla rewizji planu,
również przy równoległych POST-ach. Przegrywający request pobiera rekord
zwycięzcy, deterministycznie stosuje swój stan i zwraca ten sam `approval_id`.
Identyczny POST nie tworzy nowego wpisu ani metryki. Zmiana decyzji aktualizuje
ten sam rekord. Nowy fingerprint tworzy nową rewizję i oznacza starsze jako
`SUPERSEDED`.

## Nieaktualny plan

POST regeneruje plan i porównuje fingerprint przed walidacją decyzji. Niezgodność
zwraca HTTP 409 i niczego nie zapisuje. UI zachowuje lokalne decyzje do chwili
świadomego użycia „Odśwież plan”; nie przenosi ich automatycznie. GET approval
pokazuje poprzednią rewizję jako `SUPERSEDED`, jeżeli bieżąca nie ma approval.

## Metryki

GET planu i GET approval nie emitują metryk. Pusty `DRAFT` oraz samo
`guardrails_acknowledged=true` bez decyzji również nie są zdarzeniem DECIDED.
Jedno idempotentne `TRANSFORMATION_PLAN_DECIDED` powstaje dopiero przy pierwszej
jawnej decyzji elementu — zarówno przy INSERT, jak i późniejszym UPDATE pustego
draftu. Stabilny `operation_key` ma postać
`transformation_plan_decision:{approval_id}`, dlatego kolejne decyzje,
aktualizacje i równoległe requesty nie tworzą dalszych zdarzeń. Istniejące
agregaty Project Metrics Collector ignorują ten neutralny typ, więc globalna
semantyka liczników projektowych pozostaje bez zmian.

Migracja starej tabeli `metric_events` zachowuje rekordy i `sequence_id`,
odtwarza indeksy unikalne i wyszukiwawcze oraz append-only triggery. Ponowne
`init_models_sync` jest idempotentne i nie kopiuje ani nie usuwa historii.

## UI

Ekran pokazuje ofertę, poziomy, strategię, wersję i status; podsumowanie źródeł i
ograniczenia; pięć sekcji planu; action, reason, evidence strength i
EvidencePermission; wszystkie guardraile; decyzje radio oraz jedno potwierdzenie
guardraili. Dostępne są zapis draftu, submit, powrót i świadome odświeżenie.

## Dostępność

Sterowanie wykorzystuje natywne radio i checkbox z label, widoczny focus,
tekstowe statusy niezależne od koloru, `aria-live` dla błędów, `aria-busy` dla
loading, responsywny układ i natywne przyciski obsługiwane klawiaturą. Nie ma
modala blokującego przewijanie.

## Prywatność

Publiczne DTO nie ujawniają `metadata_json`, lifecycle/operation tokens,
identyfikatorów Truth ani surowych danych Truth. Approval zawiera tylko minimalne
identyfikatory, fingerprint, decyzje, status i daty.

## Brak LLM

Plan i approval używają wyłącznie deterministycznych usług oraz bazy danych.
Nie importują providerów LLM, generatora diffów ani generatora CV. Testy blokują
wywołanie `generate_resume_diffs`.

## API

- `GET /api/v1/jobs/{job_id}/resumes/{resume_id}/transformation-plan`;
- `POST /api/v1/jobs/{job_id}/resumes/{resume_id}/transformation-plan/approval`;
- `GET /api/v1/jobs/{job_id}/resumes/{resume_id}/transformation-plan/approval`.

HTTP 404 rozróżnia brak Job, Resume i approval. HTTP 409 oznacza stale plan.
HTTP 422 obejmuje duplikaty, nieznane referencje, brak decyzji i brak wymaganego
potwierdzenia guardraili.

## Testy

Pokrycie obejmuje fingerprint, trzy decyzje, cztery statusy, idempotencję,
stale plan, metryki, read-only Resume/Job/Application, prywatność, brak LLM,
publiczne API, klienta kontraktu oraz dostępny ekran review.

## Ograniczenia

Etap 10B nie generuje tekstu, nie stosuje decyzji do CV, nie zapisuje nowej
wersji Resume i nie eksportuje dokumentów. `REQUEST_REVIEW` nie ma jeszcze
osobnego workflow operatora.

## Ryzyka

Zmiana źródła unieważnia approval, nawet gdy widoczny fragment planu pozostaje
podobny; to świadomie konserwatywna ochrona. Etap 10C musi odrzucać `DRAFT`,
`REQUIRES_REVIEW` i `SUPERSEDED` oraz ponownie porównać fingerprint.

## Etap 10C

10C wygeneruje treść przez LLM wyłącznie według bieżącego `APPROVED` planu i
jego decyzji. 10D doda Truth Validator, diff i akceptację wygenerowanych zmian.
10E zapisze wersję oraz udostępni eksport DOCX/PDF.

## Definition of Done

- fingerprint i walidacja stale plan pozostają backendowe;
- decyzje nie zmieniają planu ani guardraili;
- zapis jest idempotentny i audytowalny;
- UI wymaga jawnych decyzji i potwierdzenia guardraili;
- Resume, Job i Application pozostają read-only;
- brak LLM i generowania CV;
- testy dedykowane, pełne suite’y, build oraz smoke są zielone.
