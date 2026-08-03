# Doplňkové doporučení pro lokálně vložené komponenty ve `fw-context`

**Projekt:** `turbyho/fw-context-mcp`  
**Typ dokumentu:** doplňkové architektonické doporučení  
**Datum:** 2026-08-03

---

## 1. Kontext

### 26.1 Kontext

V embedded projektech bývají třetí strany často vloženy přímo do zdrojového stromu:

- bez package manageru;
- bez Git submodulu;
- bez zachované vazby na upstream repository;
- bez původního tagu nebo commitu;
- s dlouhodobými lokálními úpravami;
- někdy i bez jednoznačného version headeru.

Typické příklady:

```text
lib/mbedtls/
lib/lwip/
vendor/crypto/
third_party/fs/
mbed-os/
```

Takové uspořádání samo o sobě nebrání generování SBOM ani vulnerability skenování. Zásadním problémem ale je ztráta identity a provenance komponenty.

Externí scanner nevyhodnocuje, odkud byly zdrojové soubory zkopírovány. Potřebuje dostat stabilní bezpečnostní identitu:

```text
název upstream komponenty
upstream verze
upstream repository
upstream commit nebo tag
PURL nebo CPE
lokální verze
lokální patchset
hash aktuálního source tree
```

Bez těchto údajů je výsledek Grype, Trivy, OSV-Scanneru i CVE Binary Toolu pouze heuristický.

### 26.2 Tři základní případy

#### Případ A — přesná lokální kopie upstream verze

Příklad:

```text
component: Mbed TLS
upstream version: 2.28.8
upstream commit: abcdef...
local modifications: none
```

V tomto případě lze komponentu skenovat standardně. Fyzické umístění v projektu není důležité. Důležité je, že `fw-context` vytvoří SBOM komponentu s přesnou identitou.

Doporučená evidence:

```text
identity confidence: high
version confidence: high
presence: compiled/linked/retained
security match mode: exact upstream version
```

#### Případ B — interně upravený fork známé upstream verze

Příklad:

```text
upstream: Mbed TLS 2.28.8
upstream commit: abcdef...
internal version: 2.28.8+company.4
local revision: 9822ab...
patchset: 7 patchů
```

Zde musí být odděleny dvě identity:

1. skutečná interní komponenta;
2. upstream základ použitý pro vulnerability matching.

Compliance SBOM musí popisovat skutečný fork:

```text
Mbed TLS 2.28.8+company.4
derived from Mbed TLS 2.28.8
```

Scanner projection má pro matching použít upstream verzi:

```text
Mbed TLS 2.28.8
```

Důvodem je, že scanner nemusí interní suffix správně porovnat s veřejnými version ranges.

Doporučená metadata:

```toml
[[components]]
id = "mbedtls"
name = "Mbed TLS"
type = "library"
paths = ["mbed-os/connectivity/mbedtls/**"]

version = "2.28.8+company.4"
supplier = "TrustedFirmware.org / internally maintained"

upstream_name = "mbedtls"
upstream_version = "2.28.8"
upstream_repository = "https://github.com/Mbed-TLS/mbedtls"
upstream_revision = "abcdef0123456789"

local_revision = "9822ab..."
patchset_sha256 = "..."
source_tree_sha256 = "..."

security_match_name = "mbedtls"
security_match_version = "2.28.8"
security_match_mode = "upstream-ancestor"
```

#### Případ C — původ komponenty není známý

Příklad:

```text
libs/tls/
bez Git historie
bez version headeru
bez informace o původu
lokálně upravováno několik let
```

V tomto případě není hlavním problémem scanner, ale chybějící provenance.

Nejprve je potřeba rekonstruovat:

```text
pravděpodobný upstream projekt
nejbližší upstream verzi
nejbližší upstream commit
lokální odchylky
hash aktuálního source tree
```

Do té doby má být komponenta evidována například jako:

```text
identity confidence: low
version: unknown
security match mode: heuristic
composition: incomplete
```

Release režim může takovou komponentu blokovat:

```text
ERROR — component has no stable upstream or internal identity
```

CVE Binary Tool nebo podobné nástroje mohou pomoci s heuristickou identifikací podle stringů a signatur, ale nesmí být považovány za autoritativní zdroj.

### 26.3 Doporučený datový model

Pro každou lokálně vloženou komponentu evidovat dvě skupiny údajů.

#### Skutečná interní identita

```text
component_id
name
internal version
local revision
source tree SHA-256
patchset SHA-256
supplier/maintainer
support status
```

#### Upstream bezpečnostní identita

```text
upstream name
upstream version
upstream repository
upstream revision
PURL
CPE
OSV repository identity
OSV commit
```

Příklad:

```toml
[[components]]
id = "lwip"
name = "lwIP"
type = "library"
paths = ["vendor/lwip/**"]

version = "2.1.3+company.2"
local_revision = "..."
source_tree_sha256 = "..."

upstream_name = "lwip"
upstream_version = "2.1.3"
upstream_repository = "https://github.com/lwip-tcpip/lwip"
upstream_revision = "..."
purl = "pkg:generic/lwip@2.1.3"

security_match_name = "lwip"
security_match_version = "2.1.3"
security_match_mode = "upstream-ancestor"

patchset_sha256 = "..."
patches = [
  "patches/lwip/001-timeout-fix.patch",
  "patches/lwip/002-company-port.patch",
]
```

### 26.4 Dva výstupy: compliance SBOM a scanner projection

`fw-context` by neměl posílat scanneru vždy přesně stejnou reprezentaci, kterou publikuje v compliance SBOM.

#### Compliance SBOM

Popisuje skutečně používanou komponentu:

```text
Mbed TLS 2.28.8+company.4
derived from Mbed TLS 2.28.8
patchset SHA-256: ...
source tree SHA-256: ...
```

#### Scanner projection

Normalizuje komponentu pro vulnerability matching:

```text
name: mbedtls
version: 2.28.8
PURL: upstream identity
CPE: upstream identity
internal component ID: mbedtls
artifact SHA-256: ...
```

Výhody:

- scanner nepřehlédne CVE kvůli internímu suffixu;
- veřejný SBOM nelže o tom, že jde o neupravený upstream;
- interní patchset zůstává auditovatelný;
- výsledek scanneru lze zpětně spojit s interní komponentou.

Doporučené CLI:

```bash
fw-context sbom generate \
  --output release/firmware.cdx.json

fw-context sbom scanner-projection \
  --scanner grype \
  --output release/firmware.grype.cdx.json
```

Případně implicitně:

```bash
fw-context vuln scan --scanner grype
```

kde si `fw-context` scanner projection vytvoří interně.

### 26.5 Doporučená kombinace scannerů

Pro lokálně vendored C/C++ komponenty:

```text
fw-context component registry
    + Grype version/PURL/CPE matching
    + OSV commit matching
    + fw-context patch/config/linker analysis
```

#### Grype

Primární scanner pro:

```text
name
version
PURL
CPE
```

Výstup je kandidátní sada CVE.

#### OSV-Scanner

Doplňkový scanner pro komponenty, u kterých je známý upstream repository a commit.

To může být přesnější než obecný CPE match, zejména pokud projekt používá vendored snapshot nebo Git fork.

Příklad interního exportu:

```json
{
  "packages": [
    {
      "name": "github.com/Mbed-TLS/mbedtls",
      "commit": "abcdef0123456789",
      "component_id": "mbedtls"
    }
  ]
}
```

#### CVE Binary Tool

Pouze doplňkově:

- heuristická kontrola;
- nalezení zapomenuté komponenty;
- nezávislý binary scan;
- validace starého firmware balíku.

Výsledek:

```text
evidence source: binary heuristic
confidence: low/medium
manual confirmation required: yes
```

### 26.6 Patch-aware vulnerability analysis

Scanner může správně nahlásit CVE vůči upstream verzi, i když interní fork již obsahuje backport opravy.

`fw-context` proto musí analyzovat:

```text
je upstream zranitelný soubor přítomen?
je tento soubor kompilován?
je dotčený objekt linkován?
je dotčený symbol retained?
je aktivní potřebné makro?
je přítomen opravný commit nebo ekvivalentní patch?
změnil patch relevantní řádky?
```

Možný výstup:

```text
Scanner:
  CVE-2026-12345 matches Mbed TLS 2.28.8

Internal fork:
  version: 2.28.8+company.4
  upstream ancestor: 2.28.8
  patchset: company.4

Build evidence:
  affected file compiled: yes
  affected symbol retained: yes
  vulnerable branch enabled: yes

Patch evidence:
  upstream fix commit equivalent: detected
  patch content hash: ...
  fix confidence: medium

Suggested VEX:
  fixed

Human approval required:
  yes
```

### 26.7 Rekonstrukce provenance

Pro starší lokální komponenty bez historie zavést příkaz:

```bash
fw-context sbom identify-component vendor/crypto
```

Možné kroky:

1. hledání version headerů;
2. hledání copyright a project markerů;
3. porovnání známých file names;
4. porovnání source tree proti známým upstream tagům;
5. určení nejbližšího upstream commitu;
6. vytvoření diffu;
7. vytvoření source tree hashe;
8. návrh registry položky;
9. ruční schválení.

Příkaz nesmí automaticky potvrdit identitu pouze podle podobného názvu.

Výsledek:

```text
Probable upstream: Mbed TLS
Nearest version: 2.16.12
Nearest commit: abcdef...
Files compared: 214
Exact matches: 177
Modified files: 31
Unknown files: 6
Confidence: medium
Manual review required: yes
```

### 26.8 Release policy pro lokální komponenty

#### Vývojový režim

Povolit:

```text
unknown upstream
missing PURL
heuristic identity
composition incomplete
```

Ale vždy s warningem.

#### Release režim

Vyžadovat alespoň jedno z:

```text
upstream version + revision
nebo
interní version + source tree SHA-256
```

Pro veřejné komponenty určené ke CVE matchingu navíc:

```text
security match name
security match version nebo commit
identity confidence medium/high
```

Blokující chyby:

```text
component has no stable identity
component has no version, revision or source-tree hash
security projection conflicts with compliance identity
upstream version is unknown but exact vulnerability matching is claimed
```

### 26.9 Dopad na implementační plán

Do Fáze 1 doplnit:

```text
- upstream/internal identity split;
- security_match_* fields;
- source tree SHA-256 fallback;
- identity confidence;
- scanner projection model.
```

Do Fáze 3 doplnit:

```text
- Grype scanner projection;
- match provenance;
- mapping scanner package → component_id.
```

Do Fáze 4 doplnit:

```text
- patchset evidence;
- upstream fix commit mapping;
- equivalent patch detection;
- OSV commit matching;
- VEX status fixed/not_affected.
```

Do testů doplnit fixture:

```text
tests/sbom/fixtures/
├── vendored-exact-upstream/
├── vendored-patched-fork/
├── vendored-unknown-origin/
├── vendored-backported-cve-fix/
└── vendored-version-suffix/
```

### 26.10 Doporučený závěr

Pro projekty, kde jsou všechny knihovny lokálně vložené a odpojené od upstreamu, je nejdůležitější částí řešení:

```text
obnova a dlouhodobé udržování component provenance
```

Scanner sám nedokáže spolehlivě nahradit chybějící identitu.

Doporučená architektura:

```text
components.toml
    ├── skutečná interní identita
    ├── upstream ancestor
    ├── source tree hash
    ├── patchset
    └── security scanner identity
            │
            ▼
      compliance SBOM
            +
      scanner projection
            │
            ▼
       Grype + OSV
            │
            ▼
  fw-context build/patch analysis
            │
            ▼
      člověkem schválený VEX
```

Grype tedy zůstává vhodným primárním scannerem, ale u lokálně vložených komponent musí být jeho vstup připraven a kontrolován pomocí explicitní provenance vrstvy ve `fw-context`.
