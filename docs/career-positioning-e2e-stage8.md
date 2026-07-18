# Career Positioning E2E - Stage 8

## Cel

Etap 8 zabezpiecza przepływ Career Positioning od danych wejściowych do publicznego API oraz od klienta API do rzeczywistego komponentu strony. Testy koncentrują się na dowodach, prywatności, deterministyczności i braku skutków ubocznych.

## Punkt startowy

Prace rozpoczęto na gałęzi `feature/career-positioning-e2e-stage8` od commita Etapu 7 `cb4692304e8c7480e14cc0f3e4194531cb190b2a`. Zachowano rozpoczęte testy Antigravity, a następnie poprawiono ich anonimowość, zakres asercji i zgodność z rzeczywistym interfejsem.

## Architektura testów

Backend przechodzi przez publiczny endpoint `GET /api/v1/jobs/{job_id}/career-positioning`, aplikację FastAPI uruchomioną przez transport ASGI, izolowaną bazę SQL oraz tymczasową, zanonimizowaną bibliotekę faktów. Testy nie wywołują bezpośrednio generatora raportu.

Frontend używa Vitest i React Testing Library. Renderuje rzeczywiste komponenty widoku CV oraz strony Career Positioning, a granicę HTTP zastępuje istniejącymi mockami klienta API. Dzięki temu testuje kolejność wywołań, stan ładowania, nawigację, renderowanie kontraktu backendu, odświeżanie i anulowanie requestów.

## Infrastruktura E2E w repozytorium

Repozytorium miało już pytest, `httpx.ASGITransport`, fixture `isolated_db`, Vitest, środowisko DOM i React Testing Library. Istniały również testy integracyjne publicznego API, testy strony Career Positioning oraz testy innych przepływów E2E backendu.

## Dlaczego nie użyto Playwright

Repozytorium nie posiadało Playwright ani Cypress. Nie dodano nowego ciężkiego frameworka. Wykorzystano publiczne API FastAPI oraz Vitest/React Testing Library. Prawdziwy browser E2E może zostać wykonany w osobnym Etapie 8B.

## Anonimizacja

Fixture używają wyłącznie fikcyjnych kandydatów, firm, ofert i treści. Nie zawierają rzeczywistego CV ani pełnej biblioteki faktów. Wartości prywatne są reprezentowane przez sztuczne markery, których brak w odpowiedzi jest jawnie sprawdzany.

## Scenariusz Expert

Testy potwierdzają controlled flattening dla profilu wyższego niż oferta, poziomy kandydata i oferty, lukę poziomów, strategię oraz ryzyko nadkwalifikowania. Sprawdzają również, że brak dowodów nie tworzy twierdzeń o finansach, zarządzie, budżecie, zarządzaniu ludźmi ani kompetencjach technicznych.

## Scenariusz Manager

Wariant Manager jest dostępny wyłącznie z zatwierdzonym dowodem zarządzania zespołem. Sam tytuł zawierający słowo Manager nie wystarcza. Testy obejmują czas doświadczenia kierowniczego, maksymalną wielkość zespołu, niezależność od kolejności wpisów oraz brak budżetu, coachingu i mentoringu bez dowodu.

## Scenariusz Director

Sześć przypadków parametryzowanych sprawdza osobno strategię, budżet, odpowiedzialność za wynik finansowy, skalę organizacyjną, zarządzanie menedżerami oraz współpracę z najwyższym szczeblem. Każda asercja potwierdza obecność właściwego twierdzenia i brak pozostałych twierdzeń bez odpowiadających im dowodów.

## Unapproved Truth Library entries

Osobne przypadki obejmują status niejasny, usunięty, oczekujący zatwierdzenia dokumentu, wyłączenie z CV, jawny wymóg akceptacji i brak statusu. Raport z każdym takim wpisem jest porównywany z raportem bazowym po usunięciu pól zależnych od tożsamości requestu i czasu. Porównanie obejmuje poziomy, ryzyka, kompetencje, narracje, ograniczenia i liczniki dowodów.

## Prywatność

Test API umieszcza w danych wejściowych sztuczne wartości reprezentujące dane kontaktowe, identyfikator krajowy, identyfikatory wewnętrzne, metadane i ścieżkę systemową. Żadna z tych wartości ani żaden wewnętrzny klucz nie może pojawić się w serializowanej odpowiedzi. Frontend potwierdza, że nie renderuje prywatnych markerów.

## Deterministyczność

Dwa requesty do tej samej oferty zwracają identyczne dane poza czasem generacji. Dodatkowe testy odwracają kolejność doświadczeń, kompetencji i narzędzi, a następnie porównują pełny publiczny raport poza identyfikatorem oferty i czasem generacji.

## Metryki i read-only

Przed requestem tworzony jest zanonimizowany Job, Resume i Application. Pełny stan tabel Job, Resume, Application i MetricEvent jest porównywany przed pierwszym requestem, między requestami i po drugim requeście. Stan pozostaje identyczny, co potwierdza brak zapisu i brak nowych metryk.

## Komendy uruchomienia

Backend:

```bash
cd apps/backend
.venv/bin/python -m py_compile app/routers/career_positioning.py app/schemas/career_positioning.py app/services/career_positioning.py app/services/career_positioning_report.py tests/e2e/test_career_positioning_flow.py
.venv/bin/pytest -q tests/e2e/test_career_positioning_flow.py
.venv/bin/pytest -q tests/unit/test_career_positioning.py tests/integration/test_career_positioning_api.py tests/e2e/test_career_positioning_flow.py
.venv/bin/pytest -q
```

Frontend:

```bash
cd apps/frontend
npx tsc --noEmit
npm run lint
npx vitest run tests/career-positioning-e2e.test.tsx
npx vitest run tests/api-client.test.ts tests/api-career-positioning.test.ts tests/career-positioning-page.test.tsx tests/career-positioning-e2e.test.tsx
npx vitest run
npm run build
```

## Wyniki

Dedykowany backend E2E: 27 przypadków zaliczonych. Backend Etap 7 + Etap 8: 185 testów zaliczonych. Pełny backend: 1185 testów zaliczonych, 1 test pominięty przez konfigurację i 6 ostrzeżeń bibliotek.

Dedykowany frontend integration E2E: 24 testy zaliczone. Frontend Etap 7 + Etap 8: 119 testów zaliczonych. Pełny frontend: 334 testy zaliczone. Typecheck, lint i build zakończyły się kodem zero.

## Ograniczenia

Test frontendu działa w środowisku DOM i mockuje granicę HTTP; nie uruchamia prawdziwej przeglądarki ani serwerów frontend/backend. Publiczny kontrakt API nie udostępnia surowych wartości stażu kierowniczego, dlatego ich wpływ jest weryfikowany przez publiczne ryzyko i pełną stabilność raportu.

## Ryzyka

Zmiana tekstów narracji backendu może wymagać aktualizacji precyzyjnych asercji dowodowych. Rozszerzenie kontraktu API wymaga równoległej aktualizacji parsera klienta i fixture. Testy celowo odrzucają niezatwierdzone dane, więc zmiana polityki statusów musi być jawna.

## Kandydat na Etap 8B

Etap 8B może dodać lekki zestaw browser E2E uruchamiający oba serwery, przejście od widoku CV do strony pozycjonowania, kontrolę sieci i zrzuty ekranu. Powinien powstać dopiero po świadomej decyzji o wyborze i utrzymaniu frameworka browserowego.

## Definition of Done

- Publiczne API jest testowane z izolowaną bazą i zanonimizowaną biblioteką faktów.
- Expert, Manager, Director i wszystkie warianty danych niezatwierdzonych mają jednoznaczne asercje.
- Prywatność, deterministyczność, read-only i brak metryk są sprawdzone.
- Rzeczywiste komponenty frontendu pokrywają wejście z CV, trzy requesty, raport, zakładki, refresh i anulowanie.
- Frontend nie rekonstruuje logiki backendu ani brakujących twierdzeń.
- Kontrole backendu, frontendu i zakresu kończą się kodem zero.
- Paczka audytowa zawiera wyniki i zmienione pliki bez danych prywatnych.
