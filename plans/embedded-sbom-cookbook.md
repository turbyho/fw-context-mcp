# Cookbook: generování SBOM pro embedded firmware

**Verze dokumentu:** 1.0  
**Datum:** 2026-08-03  
**Určeno pro:** embedded C/C++ projekty, zejména Mbed OS, Zephyr, PlatformIO, ESP-IDF, Arduino, CMake, Make, Keil a IAR  
**Doporučený provozní formát:** CycloneDX JSON 1.7  
**Doporučený alternativní formát:** SPDX 3.0, případně SPDX 2.3 kvůli kompatibilitě  
**Referenční implementace navržená v tomto dokumentu:** `fw-context sbom`

> Tento dokument je technický cookbook, nikoli právní stanovisko. Cílem je vytvořit auditovatelnou vazbu mezi konkrétním release artefaktem a softwarem, který byl pro danou konfiguraci skutečně sestaven a dodán.

---

## Obsah

1. [Co má být výsledkem](#1-co-má-být-výsledkem)
2. [Základní principy](#2-základní-principy)
3. [Co je komponenta](#3-co-je-komponenta)
4. [Evidence přítomnosti komponenty](#4-evidence-přítomnosti-komponenty)
5. [Doporučené formáty](#5-doporučené-formáty)
6. [Minimální metadata komponenty](#6-minimální-metadata-komponenty)
7. [Registr komponent](#7-registr-komponent)
8. [Obecný release workflow](#8-obecný-release-workflow)
9. [Recept pro Zephyr](#9-recept-pro-zephyr)
10. [Recept pro Mbed OS](#10-recept-pro-mbed-os)
11. [Recept pro PlatformIO](#11-recept-pro-platformio)
12. [Recept pro obecný CMake nebo Make projekt](#12-recept-pro-obecný-cmake-nebo-make-projekt)
13. [Návrh integrace do fw-context](#13-návrh-integrace-do-fw-context)
14. [Vulnerability scanning a VEX](#14-vulnerability-scanning-a-vex)
15. [Více image, bootloader a binární bloby](#15-více-image-bootloader-a-binární-bloby)
16. [Problematické případy](#16-problematické-případy)
17. [CI/CD pipeline](#17-cicd-pipeline)
18. [Release gate](#18-release-gate)
19. [Doporučený postup zavedení](#19-doporučený-postup-zavedení)
20. [Reference](#20-reference)

---

## 1. Co má být výsledkem

Výsledkem nemá být pouze seznam knihoven nalezených ve zdrojovém repozitáři. Výsledkem má být sada artefaktů navázaná na **konkrétní release firmware**:

```text
release/
├── firmware.bin
├── firmware.hex
├── firmware.elf
├── firmware.map
├── compile_commands.json
├── build-manifest.json
├── components-resolved.json
├── firmware.cdx.json
├── firmware.spdx.jsonld
├── firmware.vex.cdx.json
├── vulnerabilities.json
├── SHA256SUMS
└── release-attestation.json
```

Musí být možné odpovědět alespoň na tyto otázky:

1. Jaký přesný zdrojový kód byl použit?
2. Jaká konfigurace, target, board a toolchain byly použity?
3. Které komponenty byly pro danou konfiguraci skutečně kompilovány?
4. Které objekty a členy archivů vstoupily do linkování?
5. Které komponenty zůstaly ve výsledném ELF nebo image?
6. Jaká je přesná verze, revize nebo hash každé komponenty?
7. Jaké další firmware image a binární bloby jsou součástí produktu?
8. Jaké známé zranitelnosti byly nalezeny?
9. Proč je konkrétní zranitelnost relevantní, nerelevantní nebo mitigovaná?
10. Lze release později reprodukovat nebo alespoň přesně rekonstruovat?

### 1.1 Vazba na CRA

Cyber Resilience Act vyžaduje identifikaci a dokumentaci komponent produktu včetně SBOM ve strojově čitelném formátu. Prakticky je vhodné generovat a archivovat SBOM pro každý vydaný firmware, i když se seznam komponent proti předchozímu releasu nezměnil.

SBOM sám o sobě neprokazuje bezpečnost produktu. Je vstupem pro:

- správu zranitelností;
- dohledání dotčených produktů;
- řízení licencí;
- posouzení dopadu změn;
- audit release procesu;
- přípravu VEX;
- reakci na bezpečnostní incident.

---

## 2. Základní principy

## 2.1 SBOM musí odpovídat artefaktu, nikoli repozitáři

Embedded repozitář běžně obsahuje:

- více targetů a boardů;
- více implementací stejného driveru;
- podmíněně kompilované síťové stacky;
- nepoužitý middleware;
- testovací kód;
- příklady;
- utility běžící pouze na vývojovém počítači;
- kód odstraněný linkerem pomocí garbage collection;
- předkompilované knihovny;
- firmware dalších procesorů;
- různé produktové konfigurace.

Scanner celého adresářového stromu proto vytváří pouze seznam **potenciálně přítomného** kódu. Release SBOM má vycházet z konkrétního buildu.

## 2.2 Package manager není zdroj pravdy o finálním firmware

Package manager dobře odpoví na otázku:

> Co bylo nainstalováno nebo deklarováno jako závislost?

Neodpoví spolehlivě na otázku:

> Co skončilo v tomto konkrétním firmware image?

Například celý PlatformIO framework může obsahovat FreeRTOS, lwIP, Mbed TLS, Bluetooth stack a několik filesystemů, ale konkrétní image použije jen část z nich.

## 2.3 Compile database není linker database

`compile_commands.json` poskytuje:

- aktivní translation units;
- compiler flags;
- definice preprocesoru;
- include paths;
- target a ABI volby.

Neposkytuje však úplný důkaz o tom, co linker nakonec ponechal ve firmware. Pro přesnější SBOM je potřeba kombinovat jej s:

- linker mapou;
- link commandem;
- seznamem objektů a archivů;
- ELF;
- případně DWARF;
- informacemi o dalších dodávaných image.

## 2.4 Nehádejte PURL nebo CPE

Nesprávný identifikátor je horší než chybějící identifikátor, protože vede k chybnému vulnerability matchingu.

PURL nebo CPE přidávejte pouze tehdy, když je vazba jednoznačná. Pro interní nebo vendored komponentu bez veřejného package ecosystemu použijte:

- přesný název;
- supplier;
- upstream URL;
- upstream verzi;
- Git SHA;
- hash zdrojového podstromu;
- interní aliasy.

## 2.5 Generování SBOM oddělte od vulnerability scanování

Základní generátor SBOM má být:

- deterministický;
- lokální;
- použitelný offline;
- nezávislý na dostupnosti externích CVE databází;
- bez automatického odesílání informací o interním projektu.

Vulnerability scanning má být samostatný krok nad hotovým SBOM.

---

## 3. Co je komponenta

Doporučená granularita:

```text
produkt
├── aplikační firmware
├── bootloader
├── RTOS / framework
│   ├── kernel
│   ├── síťový stack
│   ├── TLS / kryptografická knihovna
│   ├── filesystem
│   └── další samostatně udržované upstream komponenty
├── MCU HAL / vendor SDK
├── middleware
├── aplikační knihovny
├── generovaný kód
├── předkompilované knihovny
├── firmware modemu / rádia / secure elementu
└── bezpečnostně významná konfigurace
```

Samostatnou komponentu vytvořte zejména tehdy, když:

- má vlastní upstream projekt;
- má vlastní release cycle;
- má vlastní verzi nebo Git revizi;
- má vlastní bezpečnostní advisories nebo CVE;
- má vlastní licenci;
- může být samostatně aktualizována;
- je dodána jako předkompilovaný blob;
- je bezpečnostně významná;
- je opakovaně používána ve více produktech.

### 3.1 Framework jako jedna komponenta versus vnořené komponenty

Pouhé uvedení:

```text
Mbed OS 6.x
```

je administrativně jednoduché, ale pro vulnerability management příliš hrubé. Mbed OS může obsahovat například:

```text
Mbed OS
├── RTX
├── Mbed TLS
├── lwIP
├── littlefs
├── CMSIS
└── vendor HAL
```

Podobně PlatformIO framework může obsahovat komponenty původem z ESP-IDF, Arduino Core, FreeRTOS, lwIP a Mbed TLS.

Doporučení:

- framework vždy uvést jako nadřazenou komponentu;
- bezpečnostně významné vložené upstream projekty uvést samostatně;
- do release SBOM zahrnout pouze ty vnořené komponenty, pro které existuje důkaz přítomnosti v konkrétním buildu.

### 3.2 Build tools

Kompilátor, linker, CMake, Ninja, SCons a Python skripty zpravidla nejsou runtime komponenty firmware. Evidujte je v build provenance.

Výjimkou jsou:

- runtime knihovny toolchainu přilinkované do image;
- generovaný runtime kód;
- části toolchainu dodávané spolu s produktem;
- build nástroj, jehož přesná verze je nutná pro audit reprodukovatelnosti.

---

## 4. Evidence přítomnosti komponenty

Používejte explicitní úrovně evidence:

| Stav | Význam |
|---|---|
| `declared` | Komponenta je uvedena v manifestu nebo konfiguraci projektu. |
| `present` | Zdrojový kód nebo balíček je přítomen ve workspace. |
| `compiled` | Alespoň jedna kompilační jednotka komponenty byla přeložena. |
| `linked` | Objekt nebo člen archivu komponenty vstoupil do linkování. |
| `retained` | Kód nebo data komponenty zůstaly ve výsledném ELF/image. |
| `deployed` | Komponenta je součástí dodaného produktu, i když nebyla linkována do hlavního ELF. |

### 4.1 Doporučené pořadí důvěryhodnosti

Od nejsilnějšího důkazu:

1. výsledný ELF a jeho sekce;
2. linker map;
3. trace linkeru;
4. seznam skutečně vytažených členů statických archivů;
5. objekty vstupující do linkování;
6. `compile_commands.json`;
7. dependency soubory `*.d`;
8. build graph CMake, Ninja, SCons nebo Make;
9. package manager metadata;
10. Git submoduly a vendored repozitáře;
11. scan celého zdrojového stromu.

### 4.2 Doporučený interní důkazní záznam

```json
{
  "source": "mbed-os/connectivity/mbedtls/library/aes.c",
  "object": "BUILD/target/GCC_ARM/mbedtls/aes.o",
  "component": "mbedtls",
  "compiled": true,
  "linked": true,
  "retained": true,
  "evidence": [
    {
      "kind": "compile-command",
      "source": "compile_commands.json",
      "record": 128
    },
    {
      "kind": "linker-map",
      "source": "firmware.map",
      "line": 8123
    }
  ]
}
```

---

## 5. Doporučené formáty

## 5.1 CycloneDX JSON

CycloneDX je vhodný jako hlavní provozní formát, protože podporuje:

- komponenty a dependency graph;
- lifecycle a provenance;
- hashe a externí reference;
- pedigree modifikovaných komponent;
- vulnerability data;
- VEX;
- kryptografická aktiva;
- compositions a vyjádření úplnosti.

Doporučené názvy:

```text
firmware.cdx.json
firmware.vex.cdx.json
```

Pro nové implementace používejte verzi podporovanou vaším navazujícím toolchainem. K datu tohoto dokumentu je aktuální CycloneDX 1.7.

## 5.2 SPDX

SPDX je vhodný zejména pro:

- detailní evidenci souborů;
- licenční informace;
- build provenance;
- nativní Zephyr `west spdx`;
- výměnu s organizacemi standardizovanými na SPDX.

Doporučení:

- SPDX 3.0 pro nový build-oriented workflow;
- SPDX 2.3 tam, kde navazující nástroje SPDX 3.0 neumějí.

## 5.3 Nepřevádějte formáty bez kontroly

CycloneDX a SPDX mají odlišný datový model. Automatická konverze může ztratit:

- build provenance;
- file-level informace;
- některé vztahy;
- licenční metadata;
- VEX informace;
- vlastní properties.

Používejte jeden interní kanonický model a z něj generujte jednotlivé výstupní formáty.

---

## 6. Minimální metadata komponenty

Každá komponenta by měla mít minimálně:

| Pole | Příklad |
|---|---|
| `name` | `mbedtls` |
| `version` | `3.6.4` |
| `revision` | plný Git SHA |
| `supplier` | `TrustedFirmware.org` |
| `type` | `library`, `framework`, `firmware`, `operating-system` |
| `source_url` | upstream Git repository |
| `download_url` | přesný zdroj tarballu nebo balíčku |
| `purl` | pouze pokud je spolehlivě určitelné |
| `cpe` | pouze pokud je spolehlivě určitelné |
| `hashes` | minimálně SHA-256 |
| `license_declared` | deklarovaná licence |
| `license_concluded` | interně ověřená licence |
| `support_status` | `supported`, `eol`, `internal-fork`, `unknown` |
| `evidence_level` | například `retained` |
| `paths` | cesty patřící komponentě |
| `patches` | interní patche a jejich hash |
| `security_sources` | advisories, mailing list, CVE feed |
| `owner` | interní vlastník komponenty |

### 6.1 Komponenta bez release verze

Použijte:

```text
version = 0+git.<short-sha>
revision = <full-git-sha>
```

### 6.2 Komponenta bez Git historie

Použijte:

```text
version = legacy+sha256.<prefix>
source_tree_sha256 = <hash normalizovaného zdrojového archivu>
```

### 6.3 Vendored komponenta s lokálními změnami

Použijte dvě identity:

```text
upstream_version = 2.28.8
internal_version = 2.28.8+company.3
upstream_revision = abcdef...
patchset_sha256 = ...
source_tree_sha256 = ...
```

Nepoužívejte neurčité hodnoty:

```text
latest
master
current
custom
unknown-version
```

---

## 7. Registr komponent

Automatická detekce nebude u embedded projektu nikdy stoprocentní. Udržujte explicitní registr, který slouží jako autoritativní override.

Pro integraci do `fw-context` je vhodný TOML, protože Python 3.11 jej umí číst bez další závislosti.

Příklad `.fw-context/components.toml`:

```toml
schema = 1

[product]
name = "example-locker-controller"
supplier = "Example s.r.o."
type = "firmware"

[[components]]
id = "app"
name = "example-locker-controller"
type = "application"
supplier = "Example s.r.o."
paths = ["src/**", "app/**", "include/**"]
version_source = "git"
git_root = "."

[components.security]
owner = "embedded-team"
support_status = "supported"

[[components]]
id = "mbed-os"
name = "Mbed OS"
type = "operating-system"
supplier = "Arm"
paths = ["mbed-os/**"]
version_source = "git"
git_root = "mbed-os"

[components.security]
support_status = "eol"
owner = "embedded-platform"

[[components]]
id = "mbedtls"
name = "Mbed TLS"
type = "library"
supplier = "TrustedFirmware.org"
parent = "mbed-os"
paths = ["mbed-os/connectivity/mbedtls/**"]
version_source = "header"
version_file = "mbed-os/connectivity/mbedtls/include/mbedtls/build_info.h"
purl_template = "pkg:generic/mbedtls@{version}"

[[components]]
id = "lwip"
name = "lwIP"
type = "library"
supplier = "lwIP project"
parent = "mbed-os"
paths = ["mbed-os/connectivity/lwipstack/**"]
version_source = "header"
version_file = "mbed-os/connectivity/lwipstack/lwip/src/include/lwip/init.h"

[[components]]
id = "modem-firmware"
name = "EG91 modem firmware"
type = "firmware"
supplier = "Quectel"
version_source = "manual"
version = "EG91EFBR06A08M4G"
deployed_always = true
```

### 7.1 Pravidla mapování

- Jeden soubor má mít jednoho primárního vlastníka.
- Vnořená komponenta má přednost před obecnější nadřazenou cestou.
- Pravidla vyhodnocujte od nejkonkrétnější cesty.
- Každý kompilovaný soubor bez vlastníka má způsobit chybu nebo release warning.
- Komponenta bez důkazu přítomnosti se do release SBOM nezahrne, pokud není označena `deployed_always`.
- Ruční override má přednost před heuristikou.
- Automatický resolver nesmí přepsat explicitně zadanou verzi nebo supplier.

---

## 8. Obecný release workflow

## 8.1 Čisté a izolované sestavení

```bash
set -euo pipefail

rm -rf build release
mkdir -p build release
```

Závislosti mají být:

- pinované na verzi;
- pinované na commit;
- nebo uložené v interním immutable artifact repository.

## 8.2 Záznam zdrojových revizí

```bash
git rev-parse HEAD > release/app.git-revision.txt
git status --porcelain=v1 > release/git-status.txt
git submodule status --recursive > release/git-submodules.txt
git diff --binary > release/uncommitted.patch
```

Release build by měl standardně selhat, pokud je pracovní strom změněn:

```bash
test -z "$(git status --porcelain=v1)" || {
    echo "Release build requires a clean Git worktree" >&2
    exit 1
}
```

## 8.3 Záznam build prostředí

Příklad `build-manifest.json`:

```json
{
  "product": "example-locker-controller",
  "release": "3.12.4",
  "build_id": "ci-18422",
  "source_revision": "0123456789abcdef",
  "board": "custom_board",
  "target": "nrf52840",
  "profile": "production",
  "build_type": "release",
  "toolchain": {
    "name": "arm-none-eabi-gcc",
    "version": "13.2.1",
    "sha256": "..."
  },
  "build_system": {
    "name": "cmake",
    "version": "3.31.6",
    "generator": "Ninja"
  },
  "flags_digest": "sha256:...",
  "configuration_digest": "sha256:...",
  "timestamp_utc": "2026-08-03T07:00:00Z"
}
```

Zaznamenejte verze nástrojů:

```bash
cmake --version > release/cmake-version.txt 2>&1 || true
ninja --version > release/ninja-version.txt 2>&1 || true
arm-none-eabi-gcc --version > release/compiler-version.txt 2>&1 || true
arm-none-eabi-ld --version > release/linker-version.txt 2>&1 || true
python3 --version > release/python-version.txt 2>&1 || true
```

## 8.4 Compilation database

CMake:

```bash
cmake -S . -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

cmake --build build --verbose

cp build/compile_commands.json release/
```

PlatformIO:

```bash
pio run -e production -t compiledb
cp compile_commands.json release/
```

Make nebo proprietární build:

- nativní export compilation database;
- Bear;
- intercept-build;
- wrapper překladače, který loguje argumenty.

Normalizujte:

- absolutní workspace cesty;
- temporary paths;
- pořadí stabilních argumentů;
- transientní build timestamp makra.

Zachovejte:

- všechny `-D`;
- všechny `-I`, `-isystem`, `-include`;
- `-mcpu`, `-mthumb`, `-mfloat-abi`;
- optimalizační volby;
- language standard;
- LTO volby.

## 8.5 Linker map

Pro GNU ld nebo lld:

```text
-Wl,-Map=firmware.map
-Wl,--cref
-Wl,--gc-sections
```

Pro diagnostický build lze doplnit:

```text
-Wl,--trace
```

Archivujte:

- ELF před `objcopy`;
- map file;
- finální BIN/HEX/UF2;
- signed nebo encrypted image;
- hash všech image;
- vztah mezi unsigned a signed image.

## 8.6 Statické archivy

```bash
find build -type f -name '*.a' -print0 |
while IFS= read -r -d '' archive; do
    {
        echo "### ${archive}"
        arm-none-eabi-ar t "${archive}"
    } >> release/static-archives.txt
done
```

Existence člena v `.a` neznamená, že byl přilinkován. Potvrďte jej linker mapou.

## 8.7 Aktivní zdroje a hlavičky

Z `compile_commands.json` vytvořte:

```text
compiled_sources
```

Z linker mapy vytvořte:

```text
linked_objects
linked_archive_members
retained_sections
```

Z `*.d` nebo libclang include graphu vytvořte:

```text
included_headers
generated_headers
```

## 8.8 Mapování na komponenty

Resolver použije:

- registr komponent;
- compile database;
- linker map;
- Git metadata;
- package manager metadata;
- version headery;
- ruční overrides.

Release gate:

```text
FAIL: kompilovaný soubor není přiřazen komponentě
FAIL: komponenta nemá jednoznačnou verzi, revizi nebo hash
FAIL: binary blob nemá SHA-256
FAIL: výsledný firmware nemá SHA-256
FAIL: dvě komponenty si nárokují stejný soubor bez priority
WARN: komponenta nemá PURL ani CPE
WARN: licence nebyla interně ověřena
WARN: komponenta je EOL
WARN: evidence končí na úrovni compiled
```

## 8.9 Root komponenta SBOM

Příklad CycloneDX:

```json
{
  "type": "firmware",
  "name": "example-locker-controller",
  "version": "3.12.4",
  "bom-ref": "urn:example:firmware:example-locker-controller:3.12.4",
  "hashes": [
    {
      "alg": "SHA-256",
      "content": "..."
    }
  ],
  "properties": [
    {
      "name": "fw-context:board",
      "value": "custom_board"
    },
    {
      "name": "fw-context:build-profile",
      "value": "production"
    },
    {
      "name": "fw-context:config-hash",
      "value": "..."
    },
    {
      "name": "fw-context:evidence-level",
      "value": "retained"
    }
  ]
}
```

## 8.10 Dependency graph

```text
firmware
├── dependsOn → application
├── dependsOn → mbed-os
├── dependsOn → mcuboot
├── dependsOn → vendor-hal
└── dependsOn → modem-firmware
```

Vnořené komponenty:

```text
mbed-os
├── contains/dependsOn → mbedtls
├── contains/dependsOn → lwip
└── contains/dependsOn → littlefs
```

Používejte stabilní `bom-ref`, aby šlo porovnávat releasy.

## 8.11 Úplnost

CycloneDX composition označte:

- `complete`, pokud jsou všechny linkované vstupy a deployed komponenty namapovány;
- `incomplete`, pokud jsou známé mezery;
- `unknown`, pokud neexistuje linkerová evidence.

Nevydávejte source-tree scan jako kompletní artefact SBOM.

## 8.12 Validace

CycloneDX:

```bash
cyclonedx-cli validate \
  --input-file release/firmware.cdx.json \
  --fail-on-errors
```

Interní kontroly:

```text
- root komponenta existuje;
- release verze odpovídá firmware;
- SHA-256 firmware odpovídá souboru;
- každý bom-ref je unikátní;
- každá dependency reference existuje;
- nejsou přítomny komponenty bez identity;
- všechny aktivní zdroje mají vlastníka;
- SBOM neobsahuje secrets;
- timestamp a build ID odpovídají release manifestu.
```

## 8.13 Hash a archivace

```bash
(
  cd release
  sha256sum \
    firmware.bin \
    firmware.elf \
    firmware.map \
    firmware.cdx.json \
    firmware.vex.cdx.json \
    build-manifest.json \
    > SHA256SUMS
)
```

Volitelně:

- podepište `SHA256SUMS`;
- podepište SBOM;
- vytvořte in-toto nebo SLSA attestation;
- uložte artefakty do immutable storage.

---

## 9. Recept pro Zephyr

Zephyr má nativní `west spdx`, který generuje SPDX dokumenty z konkrétního buildu a zaznamenává zdrojové soubory, build artefakty a jejich vztahy.

## 9.1 Příprava

Do konfigurace přidejte:

```ini
CONFIG_BUILD_OUTPUT_META=y
```

Přesně připněte projekty ve `west.yml`.

Archivujte frozen manifest:

```bash
west manifest --freeze > release/west-frozen.yml
west list -f '{name} {revision} {url}' > release/west-projects.txt
```

## 9.2 SPDX 3.0

`west spdx --init` musí proběhnout před buildem:

```bash
BUILD_DIR=build/production

rm -rf "${BUILD_DIR}"

west spdx --init -d "${BUILD_DIR}"

west build \
  -d "${BUILD_DIR}" \
  -b custom_board \
  app

west spdx \
  -d "${BUILD_DIR}" \
  --spdx-version 3.0 \
  --analyze-includes \
  --include-sdk
```

Výstup:

```text
build/production/spdx/
├── app.jsonld
├── zephyr.jsonld
├── build.jsonld
├── modules-deps.jsonld
└── sdk.jsonld
```

## 9.3 Co archivovat

```bash
cp "${BUILD_DIR}/zephyr/.config" release/zephyr.config
cp "${BUILD_DIR}/zephyr/zephyr.dts" release/zephyr.dts
cp "${BUILD_DIR}/zephyr/zephyr.map" release/firmware.map
cp "${BUILD_DIR}/zephyr/zephyr.elf" release/firmware.elf
cp "${BUILD_DIR}/zephyr/zephyr.bin" release/firmware.bin
```

Dále:

- overlays;
- `devicetree_generated.h`;
- `autoconf.h`;
- signed image;
- partition map;
- PM static configuration;
- west patches;
- binární bloby.

## 9.4 Sysbuild a multi-image

Každý image má vlastní SBOM:

```text
device-product
├── application-image
├── mcuboot-image
├── network-core-image
└── secure-image
```

U sysbuild generujte nebo importujte SBOM pro každý skutečný build adresář a následně vytvořte nadřazený product BOM.

## 9.5 Doplnění nativního `west spdx`

Doplňte:

- produktové označení;
- release verzi;
- hash signed image;
- modemový/radio firmware;
- secure element firmware;
- podporu a EOL status;
- interní patche;
- PURL/CPE aliasy;
- VEX;
- vazbu na konkrétní `.config` a Devicetree;
- interní vlastnictví komponent.

## 9.6 Zephyr release gate

```text
FAIL: west manifest není frozen
FAIL: workspace obsahuje dirty module
FAIL: není archivována .config
FAIL: není archivován výsledný Devicetree
FAIL: sysbuild image nemá vlastní SBOM
FAIL: blob nemá identifikátor, licenci a SHA-256
WARN: CVE je posuzováno pouze podle verze Zephyru bez kontroly Kconfigu
```

---

## 10. Recept pro Mbed OS

Arm oznámil konec životního cyklu Mbed OS v červenci 2026. Pro release proces jej proto považujte za legacy platformu, kterou musí výrobce interně udržovat nebo migrovat.

## 10.1 Co připnout

Archivujte:

```text
- commit Mbed OS;
- commit aplikace;
- všechny .lib a Git dependencies;
- target;
- toolchain;
- build profile;
- mbed_app.json;
- custom_targets.json;
- linker script;
- lokální patche;
- verzi Pythonu;
- verzi Mbed CLI/build tools;
- zdrojový archiv Mbed OS;
- zdrojový archiv build tools.
```

## 10.2 Build

Mbed CLI 1:

```bash
bear --output compile_commands.json -- \
  mbed compile \
    -m CUSTOM_TARGET \
    -t GCC_ARM \
    --profile release \
    --clean \
    -v
```

U jiné verze build tools zachovejte produkční příkaz. Nevytvářejte zvláštní SBOM build s jinou konfigurací.

## 10.3 Build evidence

Použijte kombinaci:

1. verbose build log;
2. `compile_commands.json`;
3. `*.d`;
4. linker map;
5. výsledný ELF;
6. obsah statických archivů;
7. registr komponent.

Zajistěte:

```text
-Wl,-Map=firmware.map
-Wl,--cref
```

Skutečný linker command ověřte ve verbose logu.

## 10.4 Rozložení Mbed OS

Typický strom:

```text
mbed-os
├── RTX
├── mbedtls
├── lwip
├── littlefs
├── nanostack
├── CMSIS
├── MCU vendor HAL
└── target-specific drivers
```

Identitu komponenty určujte v tomto pořadí:

1. samostatný Git submodule nebo vendored Git metadata;
2. upstream version header;
3. release metadata;
4. Mbed component metadata;
5. ruční override;
6. hash zdrojového podstromu.

## 10.5 Interní fork

Příklad:

```text
upstream: mbedtls 2.28.8
internal version: 2.28.8+company.3
upstream revision: abcdef...
patch set revision: company-patches@4f281...
source tree sha256: ...
```

## 10.6 EOL metadata

```toml
[components.security]
support_status = "internal-fork"
owner = "embedded-security"
monitoring = [
  "NVD",
  "OSV",
  "upstream component advisories",
  "vendor advisories"
]
remediation = "backport-or-replace"
migration_target = "Zephyr"
```

## 10.7 Mbed OS release gate

```text
FAIL: není uložen přesný Mbed OS commit
FAIL: není uložen zdrojový archiv legacy dependency
FAIL: dependency ukazuje na branch
FAIL: kompilovaný soubor není přiřazen komponentě
FAIL: patchovaná komponenta používá pouze upstream verzi
WARN: komponenta nemá aktivní upstream security feed
WARN: Mbed OS je označen jako supported
```

---

## 11. Recept pro PlatformIO

PlatformIO poskytuje dobrou evidenci packages, ale jejich seznam není runtime SBOM.

## 11.1 Připnutí závislostí

Nevhodné:

```ini
[env:production]
platform = espressif32
lib_deps =
    ArduinoJson
```

Vhodnější:

```ini
[env:production]
platform = platformio/espressif32@6.10.0
framework = arduino
board = esp32dev

lib_deps =
    bblanchon/ArduinoJson@7.4.2
```

Git dependency:

```ini
lib_deps =
    https://github.com/example/example-lib.git#0123456789abcdef0123456789abcdef01234567
```

## 11.2 Environment je součást identity buildu

Každý environment musí mít vlastní SBOM:

```text
release/
├── production-nrf52/
│   └── firmware.cdx.json
├── production-esp32/
│   └── firmware.cdx.json
└── diagnostic/
    └── firmware.cdx.json
```

Pro release nepouštějte neurčité `pio run` nad více environmenty bez jasného mapování výstupů.

## 11.3 Package a project metadata

```bash
ENV=production

pio --version > release/platformio-version.txt

pio pkg list \
  -e "${ENV}" \
  --verbose \
  > release/platformio-packages.txt

pio project metadata \
  -e "${ENV}" \
  --json-output-path release/platformio-metadata.json
```

## 11.4 Compilation database

```bash
pio run -e "${ENV}" -t clean
pio run -e "${ENV}" -t compiledb
pio run -e "${ENV}" -v 2>&1 | tee release/platformio-build.log
```

PlatformIO umožňuje pomocí advanced scriptu nastavit:

```python
Import("env")

env.Replace(COMPILATIONDB_INCLUDE_TOOLCHAIN=True)
env.Replace(COMPILATIONDB_PATH="$BUILD_DIR/compile_commands.json")
```

## 11.5 Linker map

```python
# sbom/platformio_linker_map.py
Import("env")

map_path = env.subst("$BUILD_DIR/${PROGNAME}.map")

env.Append(
    LINKFLAGS=[
        f"-Wl,-Map={map_path}",
        "-Wl,--cref",
    ]
)
```

`platformio.ini`:

```ini
[env:production]
extra_scripts =
    pre:sbom/platformio_linker_map.py
```

Ověřte skutečný linker command ve verbose logu.

## 11.6 Složení evidence

```text
pio pkg list
    ↓
resolved packages

pio project metadata
    ↓
toolchain, program path, flags a environment

compile_commands.json
    ↓
skutečně kompilované zdroje

firmware.map
    ↓
linkované a retained objekty

components.toml
    ↓
normalizovaná identita, supplier, licence a upstream
```

## 11.7 Framework decomposition

Package:

```text
framework-arduinoespressif32
```

může obsahovat:

- FreeRTOS;
- lwIP;
- Mbed TLS;
- ESP-IDF komponenty;
- Bluetooth stack;
- filesystemy;
- USB stack.

Pro bezpečnostně významné části vytvořte samostatné komponenty, pokud jsou skutečně přítomné.

## 11.8 PlatformIO release gate

```text
FAIL: platform není pinovaná
FAIL: lib_deps používá pohyblivou branch
FAIL: environment nemá vlastní SBOM
FAIL: chybí compile_commands.json
FAIL: chybí linker map
WARN: package list je jediný zdroj
WARN: framework není rozložen na významné komponenty
```

---

## 12. Recept pro obecný CMake nebo Make projekt

## 12.1 CMake File API

Kromě `compile_commands.json` lze využít CMake File API pro:

- target graph;
- zdrojové soubory targetu;
- link fragments;
- output artefakty;
- toolchain metadata.

Doporučený sběr:

```text
.cmake/api/v1/query/
└── codemodel-v2

build/.cmake/api/v1/reply/
├── codemodel-*.json
├── target-*.json
└── toolchains-*.json
```

## 12.2 Ninja

```bash
ninja -C build -t commands > release/ninja-commands.txt
ninja -C build -t targets all > release/ninja-targets.txt
ninja -C build -t deps > release/ninja-deps.txt
```

## 12.3 Make

```bash
make clean
bear -- make -j
make V=1 2>&1 | tee release/build.log
```

U custom buildu je vhodné doplnit wrapper:

```text
compiler-wrapper
linker-wrapper
archiver-wrapper
```

Wrapper zaznamená:

- cwd;
- argv;
- environment allowlist;
- vstupy;
- výstupy;
- exit status;
- hash vstupů a výstupů.

---

## 13. Návrh integrace do fw-context

`fw-context` je pro generování embedded SBOM přirozené místo, protože již pracuje s aktivním buildem, `compile_commands.json`, libclang AST, aktivními makry, include graphem, hashi zdrojů a klasifikací project/vendor.

SBOM nemá být implementován jako obecný directory scanner. Má být další build-aware vrstvou nad existujícím indexem.

## 13.1 Cíl funkce

Navrhované základní použití:

```bash
fw-context index --build

fw-context sbom generate \
  --artifact BUILD/target/GCC_ARM/firmware.elf \
  --map BUILD/target/GCC_ARM/firmware.map \
  --output release/firmware.cdx.json
```

Automatický režim:

```bash
fw-context sbom generate \
  --release-version 3.12.4 \
  --output release/firmware.cdx.json
```

Kontroly:

```bash
fw-context sbom check
fw-context sbom validate release/firmware.cdx.json
fw-context sbom diff old.cdx.json new.cdx.json
fw-context sbom explain mbedtls
```

## 13.2 Architektura

```text
compile_commands.json
fw-context manifest.json
SQLite semantic index
build-system adapter
linker map
ELF
component registry
package metadata
Git metadata
        │
        ▼
canonical SBOM model
        │
        ├── CycloneDX JSON exporter
        ├── SPDX exporter/importer
        ├── evidence report
        └── VEX evidence bundle
```

Navržená struktura:

```text
src/fw_context_mcp/
├── sbom/
│   ├── __init__.py
│   ├── model.py
│   ├── collector.py
│   ├── resolver.py
│   ├── evidence.py
│   ├── registry.py
│   ├── validation.py
│   ├── diff.py
│   ├── adapters/
│   │   ├── base.py
│   │   ├── generic.py
│   │   ├── zephyr.py
│   │   ├── mbed_os.py
│   │   └── platformio.py
│   ├── linker/
│   │   ├── base.py
│   │   ├── gnu_map.py
│   │   ├── lld_map.py
│   │   ├── armclang_map.py
│   │   ├── iar_map.py
│   │   └── elf.py
│   └── exporters/
│       ├── cyclonedx.py
│       └── spdx.py
└── cli.py
```

## 13.3 Kanonický interní model

Exportéry nemají číst přímo SQLite ani linker mapu. Nejprve vytvořte neutrální model:

```python
@dataclass(frozen=True)
class ComponentIdentity:
    component_id: str
    name: str
    version: str
    revision: str | None
    supplier: str | None
    component_type: str
    purl: str | None
    cpe: str | None
    source_url: str | None
    hashes: tuple[HashValue, ...]
    licenses: tuple[str, ...]

@dataclass(frozen=True)
class ComponentEvidence:
    level: str
    source_paths: tuple[str, ...]
    object_paths: tuple[str, ...]
    archive_members: tuple[str, ...]
    retained_sections: tuple[str, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    confidence: str

@dataclass(frozen=True)
class FirmwareArtifact:
    path: str
    sha256: str
    artifact_type: str
    image_role: str
    signed: bool

@dataclass(frozen=True)
class SbomGraph:
    root: ComponentIdentity
    components: tuple[ComponentIdentity, ...]
    relationships: tuple[Relationship, ...]
    evidence: Mapping[str, ComponentEvidence]
    artifacts: tuple[FirmwareArtifact, ...]
    completeness: str
```

## 13.4 Využití existujícího fw-context manifestu

Existující manifest již poskytuje:

- translation units;
- normalizované argumenty;
- source hashes;
- included header hashes;
- aktivní macros;
- `config_hash`;
- build directory patterns.

Pro SBOM doplňte nový `sbom-input.json`:

```json
{
  "_format": "fw-context-sbom-input/1",
  "config_hash": "...",
  "manifest_sha256": "...",
  "component_registry_sha256": "...",
  "artifact_sha256": "...",
  "linker_map_sha256": "...",
  "build_system": "mbed-os",
  "build_target": "CUSTOM_TARGET",
  "build_profile": "release"
}
```

SBOM cache key nesmí být pouze `config_hash`. Stejná compile configuration může vytvořit jiný image například při změně zdrojového kódu nebo generovaných dat.

Použijte:

```text
sbom_input_hash = SHA256(
    config_hash
    + manifest_sha256
    + component_registry_sha256
    + artifact_sha256
    + linker_map_sha256
)
```

## 13.5 Rozšíření build-system protocolu

Stávající build adaptery již detekují a sestavují Mbed OS, Zephyr a PlatformIO. Rozšiřte protocol o:

```python
class BuildSystem(Protocol):
    def discover_release_artifacts(
        self,
        project_root: Path,
        build_config: BuildConfig,
    ) -> list[ReleaseArtifact]:
        ...

    def collect_package_metadata(
        self,
        project_root: Path,
        build_config: BuildConfig,
    ) -> list[PackageCandidate]:
        ...

    def suggest_component_rules(
        self,
        project_root: Path,
        build_config: BuildConfig,
    ) -> list[ComponentRule]:
        ...
```

`ReleaseArtifact`:

```python
@dataclass(frozen=True)
class ReleaseArtifact:
    role: str
    elf: Path | None
    binary: Path | None
    hex: Path | None
    map_file: Path | None
    link_command: tuple[str, ...] | None
    environment: str | None
    board: str | None
```

## 13.6 Adapter Zephyr

Má získat:

- `west manifest --freeze`;
- `west list`;
- `.config`;
- Devicetree;
- `zephyr.elf`;
- `zephyr.map`;
- `zephyr.bin`;
- sysbuild child image;
- blob declarations;
- nativní `west spdx` output, pokud existuje.

Režimy:

```text
native-spdx-import
fw-context-generated-cyclonedx
hybrid-merge
```

V hybridním režimu:

- Zephyr SPDX zůstane file/build provenance zdrojem;
- `fw-context` doplní produktový root, komponentní identitu, deployed firmware, interní metadata a VEX vazby.

## 13.7 Adapter Mbed OS

Má získat:

- `.mbed`;
- `mbed_app.json`;
- `custom_targets.json`;
- build profile;
- Mbed OS Git SHA;
- `.lib` dependencies;
- `BUILD/**.elf`;
- `BUILD/**.map`;
- active source paths;
- version headery vnořených upstream komponent.

U více nalezených image nesmí automaticky vybrat první. Musí:

- použít target/profile z aktivní konfigurace;
- nebo vyžadovat jednoznačný `artifact` v configu;
- nebo vygenerovat samostatný SBOM pro každý image.

## 13.8 Adapter PlatformIO

Do `BuildConfig` doplňte explicitní environment:

```toml
[build]
system = "platformio"
environment = "production"
```

Adapter získá:

- computed project config;
- project metadata JSON;
- package list;
- `.pio/build/<env>/firmware.elf`;
- map file;
- package manifesty;
- `.pio/libdeps/<env>`;
- framework metadata.

Bez environmentu má `fw-context sbom` selhat, pokud projekt obsahuje více environmentů.

## 13.9 Linker parser

MVP podporuje GNU ld map.

Výstup parseru:

```python
@dataclass(frozen=True)
class LinkedInput:
    object_path: str
    archive_path: str | None
    archive_member: str | None
    output_sections: tuple[str, ...]
    retained_size: int
    discarded: bool
```

Parser musí rozlišit:

- standalone `.o`;
- `archive.a(member.o)`;
- discarded input sections;
- load-only metadata;
- COMMON;
- generated linker stubs;
- LTO plugin objekty;
- absolutní a relativní cesty.

### Pravidlo pro `retained`

Komponenta je `retained`, pokud alespoň jeden její vstup přispěl nenulovou částí do alokované sekce výsledného image.

Data jako debug sekce se do runtime presence standardně nepočítají.

Konfigurovatelný allowlist runtime sekcí:

```toml
[sbom.linker]
runtime_sections = [
  ".text*",
  ".rodata*",
  ".data*",
  ".bss*",
  ".noinit*",
  ".ramfunc*",
  ".vectors*",
  ".init_array*",
  ".fini_array*"
]
```

## 13.10 Component resolver

Pořadí autority:

1. explicitní pravidlo v `components.toml`;
2. build-system package metadata;
3. samostatný Git repository root;
4. Git submodule;
5. package manifest;
6. version header;
7. známá framework heuristika;
8. hash zdrojového podstromu.

Výsledek resolveru musí obsahovat i diagnostiku:

```text
mbedtls
  identity: explicit registry
  version: header MBEDTLS_VERSION_STRING
  revision: inherited from mbed-os Git tree
  evidence: retained
  sources: 18
  objects: 18
  retained bytes: 42 816
  warnings:
    - local source differs from upstream release tarball
```

## 13.11 Soubor versus komponenta

Stávající klasifikace `is_project` je pro SBOM příliš hrubá. Přidejte:

```text
files.component_id
files.component_evidence
files.package_root
```

Alternativně udržujte SBOM data v samostatných tabulkách:

```sql
CREATE TABLE sbom_components (
    config_hash TEXT NOT NULL,
    component_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    supplier TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL,
    purl TEXT NOT NULL DEFAULT '',
    cpe TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (config_hash, component_id)
);

CREATE TABLE sbom_file_components (
    config_hash TEXT NOT NULL,
    file_path TEXT NOT NULL,
    component_id TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (config_hash, file_path, component_id)
);

CREATE TABLE sbom_artifacts (
    sbom_input_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (sbom_input_hash, role, path)
);
```

Samostatné tabulky jsou vhodnější, protože:

- jedna konfigurace může mít více product image;
- component identity se může lišit podle release metadata;
- SBOM lze regenerovat bez reindexace AST;
- linker evidence má jiný lifecycle než symbol index.

## 13.12 CLI konfigurace

`.fw-context/config.toml`:

```toml
[sbom]
format = "cyclonedx-json"
spec_version = "1.7"
component_registry = ".fw-context/components.toml"
output = "release/firmware.cdx.json"
fail_unmapped = true
minimum_evidence = "compiled"
include_headers = false
include_build_tools = true
include_source_paths = false
generate_vex_template = true

[sbom.product]
name = "example-locker-controller"
supplier = "Example s.r.o."
version_command = "git describe --tags --always --dirty"

[sbom.artifact]
elf = "BUILD/CUSTOM_TARGET/GCC_ARM/firmware.elf"
map = "BUILD/CUSTOM_TARGET/GCC_ARM/firmware.map"
binary = "BUILD/CUSTOM_TARGET/GCC_ARM/firmware.bin"
role = "application"
```

## 13.13 Doporučené příkazy

```bash
fw-context sbom init
```

Vytvoří základní `components.toml` z detekovaných Git repositories, package metadata a framework heuristik.

```bash
fw-context sbom check
```

Zobrazí:

- nenamapované zdroje;
- konfliktní pravidla;
- komponenty bez identity;
- EOL komponenty;
- chybějící artefakty;
- evidence level;
- míru úplnosti.

```bash
fw-context sbom generate
```

Vytvoří SBOM a evidence report.

```bash
fw-context sbom explain mbedtls
```

Vysvětlí, proč je komponenta v SBOM.

```bash
fw-context sbom diff old.cdx.json new.cdx.json
```

Zobrazí:

- přidané a odebrané komponenty;
- změny verzí;
- změny support statusu;
- změnu evidence;
- změnu artifact hash;
- změnu configuration hash.

## 13.14 MCP nástroje

Doporučené read-only MCP tools:

```text
get_sbom_summary
list_firmware_components
get_component
get_component_evidence
find_component_files
compare_sbom_releases
get_unmapped_build_inputs
assess_vulnerability_context
```

`assess_vulnerability_context` nemá automaticky rozhodnout, že produkt není zranitelný. Má připravit důkazy:

- je dotčená komponenta přítomna;
- je dotčený zdroj kompilován;
- je symbol přítomen;
- je funkce dosažitelná;
- jaká konfigurace je aktivní;
- jaké call paths existují;
- zda je modul pouze klient/server;
- zda byla aplikována lokální oprava.

## 13.15 Největší přidaná hodnota fw-context: VEX evidence

Běžný SBOM scanner ví:

```text
firmware obsahuje mbedtls 2.28.8
```

`fw-context` může dodat:

```text
- zranitelný soubor není v compile_commands;
- zranitelný symbol není v indexu;
- příslušná preprocesorová větev není aktivní;
- serverový modul není kompilován;
- linker map neobsahuje daný objekt;
- call graph neobsahuje dosažitelnou cestu z externího vstupu;
- lokální patch mění dotčenou funkci.
```

To je vhodný podklad pro VEX, ale finální bezpečnostní rozhodnutí musí být schváleno člověkem.

## 13.16 Offline a privacy pravidla

Výchozí chování:

```text
fw-context sbom generate
```

nesmí:

- volat externí služby;
- odesílat názvy interních komponent;
- stahovat CVE databáze;
- odhadovat veřejnou identitu pomocí cloudového LLM.

Online enrichment musí být explicitní:

```bash
fw-context sbom enrich --online
```

## 13.17 Závislosti implementace

MVP lze vytvořit pouze se standardní knihovnou:

- `tomllib`;
- `json`;
- `hashlib`;
- `subprocess`;
- `sqlite3`;
- `pathlib`;
- `xml` pouze pokud bude potřeba.

Volitelné dependencies:

```toml
[project.optional-dependencies]
sbom = [
    "pyelftools>=0.31",
    "packageurl-python>=0.16",
]
sbom-validation = [
    "jsonschema>=4.22",
]
```

Nezavádějte těžkou povinnou závislost pouze kvůli exportu několika používaných částí CycloneDX.

## 13.18 Fáze implementace

### Fáze A — build-aware component inventory

- `components.toml`;
- komponenty z aktivních translation units;
- Git a package metadata;
- CycloneDX JSON;
- evidence maximálně `compiled`;
- composition `incomplete`.

### Fáze B — linker evidence

- GNU ld map parser;
- ELF a artifact hashes;
- `linked` a `retained`;
- completeness report;
- release gate.

### Fáze C — framework adapters

- Zephyr native SPDX import;
- sysbuild;
- PlatformIO environment;
- Mbed nested components;
- blobs a další firmware image.

### Fáze D — VEX workflow

- vulnerability import;
- evidence bundle;
- MCP applicability queries;
- CycloneDX VEX template;
- schvalovací metadata.

### Fáze E — provenance a signing

- build formulation;
- in-toto/SLSA attestation;
- podpis SBOM;
- immutable release archive.

## 13.19 Testovací strategie

Fixture projekty:

```text
tests/sbom/fixtures/
├── generic-cmake/
├── mbed-os/
├── platformio/
├── zephyr-single-image/
├── zephyr-sysbuild/
├── gnu-map/
├── gnu-map-lto/
└── binary-blob/
```

Testy:

```text
- správné mapování archive.a(member.o);
- discarded sekce nejsou retained;
- LTO generuje warning;
- více PlatformIO environmentů vyžaduje výběr;
- sysbuild vytvoří více image;
- dirty Git tree je zachycen;
- nested component má přednost před frameworkem;
- chybějící verze způsobí FAIL;
- output odpovídá JSON schema;
- dva identické vstupy vytvoří stejný obsah komponent a vztahů;
- změna artefaktu změní sbom_input_hash.
```

Golden files používejte pouze pro stabilní podmnožinu. Náhodný CycloneDX serial normalizujte při testu.

---

## 14. Vulnerability scanning a VEX

## 14.1 Scan hotového SBOM

Grype:

```bash
grype release/firmware.cdx.json \
  --output json \
  > release/vulnerabilities.grype.json
```

OSV-Scanner lze použít nad podporovaným SBOM formátem; ověřte syntax konkrétní instalované major verze.

Výsledky scanneru nejsou konečný verdikt.

## 14.2 Proč embedded vytváří false positives

- komponenta je vendored fork;
- lokální oprava není vyjádřena změnou upstream verze;
- zranitelný modul není kompilován;
- kód byl odstraněn linkerem;
- feature je vypnuta konfigurací;
- zranitelnost platí pouze pro server;
- dotčený protokol není externě dosažitelný;
- scanner použil chybný CPE;
- vendor verze neodpovídá upstream release.

## 14.3 VEX záznam

```yaml
vulnerability: CVE-2026-12345
component: mbedtls@2.28.8+company.3
status: not_affected
justification: vulnerable_code_not_present
analysis:
  detail: >
    Zranitelnost je v TLS server session resumption.
    Produkt obsahuje pouze TLS klienta. Serverové zdroje nejsou kompilovány
    a nejsou přítomny v linker mapě.
  evidence:
    - mbed_app.json
    - compile_commands.json
    - firmware.map
    - fw-context-query:SEC-1842
reviewed_by: security-team
reviewed_at: 2026-08-03
```

## 14.4 Povolené stavy

| Stav | Význam |
|---|---|
| `affected` | Produkt je zranitelný. |
| `not_affected` | Zranitelnost se na produkt nevztahuje. |
| `fixed` | Zranitelnost byla opravena. |
| `under_investigation` | Analýza není dokončena. |

## 14.5 Doporučená zdůvodnění

```text
vulnerable_code_not_present
vulnerable_code_not_in_execute_path
vulnerable_code_cannot_be_controlled_by_adversary
inline_mitigations_already_exist
```

`not_affected` nesmí být založeno pouze na tvrzení:

```text
tuto funkci pravděpodobně nepoužíváme
```

Musí existovat build-aware důkaz.

---

## 15. Více image, bootloader a binární bloby

Embedded produkt často obsahuje více samostatných softwarových jednotek:

```text
produkt
├── application MCU image
├── network core image
├── secure firmware
├── bootloader
├── modem firmware
├── radio co-processor firmware
├── FPGA bitstream
└── manufacturing configuration
```

Každý samostatně sestavený nebo aktualizovatelný image má mít vlastní komponentu a hash.

## 15.1 Hierarchický BOM

Vhodný model:

```text
product BOM
├── application firmware BOM
├── bootloader BOM
├── network core BOM
└── modem firmware component
```

## 15.2 Binární blob bez zdrojů

Evidujte:

- supplier;
- přesný název;
- verzi;
- SHA-256;
- download/source URL;
- licenci;
- export restrictions;
- support status;
- security contact;
- update mechanism;
- zda je blob kritický pro boot nebo komunikaci.

## 15.3 Firmware zjištěný až na zařízení

Například modemová verze nemusí být součástí build workspace. Získávejte ji z:

- manufacturing inventory;
- AT commandu;
- production logu;
- device attestation;
- provisioning backendu.

Taková komponenta je `deployed`, nikoli `compiled`.

---

## 16. Problematické případy

## 16.1 LTO

Při LTO může linker map obsahovat:

```text
ltrans.o
```

místo původních objektů. Doporučení:

- zachovat compile evidence;
- zachytit link command a vstupní archivy;
- použít plugin map nebo save-temps, pokud je k dispozici;
- označit granularitu `retained` jako částečně odvozenou;
- nevydávat composition jako complete, pokud nelze původ spolehlivě určit.

## 16.2 Header-only knihovny

Header-only knihovna může být runtime komponentou, i když nemá vlastní `.o`.

Důkaz:

- header je zahrnut aktivním translation unit;
- template nebo inline kód je instancován;
- symboly nebo DWARF ukazují použití;
- komponenta je bezpečnostně významná.

Pro základní SBOM může být `compiled` evidence založena na aktivním include graphu.

## 16.3 Amalgamated source

Jedna `library.c` může obsahovat několik upstream částí. Registr musí umožnit:

- path + line range;
- generated source provenance;
- component override na úrovni symbolu;
- nebo označení jako interní amalgamated component s pedigree.

## 16.4 Generovaný kód

Evidujte:

- generátor;
- verzi generátoru;
- input schema;
- hash generated source;
- zda je generated source součástí SBOM jako file;
- runtime komponentu, ke které kód patří.

## 16.5 Patche bez změny verze

Nikdy neponechávejte pouze upstream verzi. Přidejte:

```text
+company.N
patchset hash
source tree hash
```

Jinak vulnerability scanner i auditor předpokládá čistou upstream verzi.

## 16.6 Stripped ELF

Uchovávejte:

- interní unstripped ELF;
- zákaznický stripped image;
- hash obou;
- vztah mezi nimi.

## 16.7 Encrypted image

SBOM vztahujte k plaintext build artefaktu a současně archivujte hash distribuovaného encrypted image.

```text
firmware.elf
  → firmware.bin
  → firmware.signed.bin
  → firmware.encrypted.bin
```

## 16.8 Dynamicky načítané moduly

Pokud produkt podporuje:

- pluginy;
- skripty;
- WebAssembly;
- dynamicky nahrávaný radio firmware;
- externí aplikační balíčky,

musí SBOM pokrývat i tyto runtime komponenty nebo definovat, jak jsou evidovány samostatně.

---

## 17. CI/CD pipeline

Příklad:

```yaml
name: firmware-release

on:
  push:
    tags:
      - "v*"

jobs:
  build-and-sbom:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4
        with:
          submodules: recursive
          fetch-depth: 0

      - name: Build and index
        run: |
          fw-context index --build

      - name: Generate SBOM
        run: |
          fw-context sbom check
          fw-context sbom generate \
            --release-version "${GITHUB_REF_NAME}" \
            --output release/firmware.cdx.json

      - name: Validate SBOM
        run: |
          cyclonedx-cli validate \
            --input-file release/firmware.cdx.json \
            --fail-on-errors

      - name: Vulnerability scan
        run: |
          grype release/firmware.cdx.json \
            --output json \
            > release/vulnerabilities.json

      - name: Release checksums
        run: |
          cd release
          sha256sum * > SHA256SUMS

      - uses: actions/upload-artifact@v4
        with:
          name: firmware-release-evidence
          path: release/
```

### 17.1 CI musí pinovat nástroje

Nepoužívejte nekontrolované:

```text
latest
main
master
```

Pinujte:

- container digest;
- fw-context verzi;
- CycloneDX validator;
- vulnerability scanner;
- toolchain;
- build image;
- Zephyr SDK;
- PlatformIO Core;
- Mbed build tools.

### 17.2 Vulnerability gate

Nedoporučuje se jednoduché pravidlo:

```text
FAIL pokud scanner najde libovolné CVE
```

Lepší:

```text
FAIL pokud:
- je nový nález se stavem affected;
- je překročen termín under_investigation;
- chybí vlastník;
- chybí VEX rozhodnutí pro kritickou zranitelnost;
- scanner database je příliš stará;
- komponenta nemá identitu dostatečnou pro matching.
```

---

## 18. Release gate

### 18.1 Povinné

```text
[ ] čistý nebo zdokumentovaný source tree
[ ] přesný release tag a Git SHA
[ ] pinované závislosti
[ ] archivovaný compile_commands.json
[ ] archivovaný build manifest
[ ] archivovaný linker map
[ ] archivovaný ELF
[ ] SHA-256 finálního distribuovaného image
[ ] všechny aktivní zdroje namapovány na komponenty
[ ] všechny deployed bloby evidovány
[ ] každá komponenta má verzi, revizi nebo hash
[ ] SBOM prošel schema validací
[ ] SBOM composition odpovídá skutečné míře úplnosti
[ ] vulnerability scan archivován
[ ] kritické nálezy mají rozhodnutí a vlastníka
```

### 18.2 Doporučené

```text
[ ] source archive všech EOL dependencies
[ ] interní patchset archive
[ ] build container digest
[ ] podpis SBOM
[ ] VEX dokument
[ ] reproducibility test
[ ] diff proti předchozímu releasu
[ ] licence a copyright evidence
[ ] build provenance attestation
```

---

## 19. Doporučený postup zavedení

### Etapa 1: inventory

Cíl:

- identifikovat všechny top-level komponenty;
- zavést `components.toml`;
- archivovat verze a Git SHA;
- vygenerovat první CycloneDX SBOM z aktivních translation units.

Acceptovatelná omezení:

- evidence pouze `compiled`;
- composition `incomplete`;
- manuální doplnění modemového firmware a blobů.

### Etapa 2: linker evidence

Cíl:

- povinný linker map;
- GNU map parser;
- rozlišení `linked` a `retained`;
- fail na nenamapované link inputs.

### Etapa 3: framework-specific metadata

Cíl:

- Zephyr `west spdx`;
- PlatformIO environment a packages;
- Mbed nested upstream komponenty;
- sysbuild a více image.

### Etapa 4: vulnerability management

Cíl:

- automatický scan při releasu;
- CVE triage workflow;
- VEX;
- evidence z `fw-context`.

### Etapa 5: compliance-grade release archive

Cíl:

- podpisy;
- attestations;
- immutable retention;
- pravidelné ověřování reprodukovatelnosti;
- vazba na produktovou a výrobní evidenci.

---

## 20. Reference

### Právní rámec

- Cyber Resilience Act, nařízení (EU) 2024/2847:  
  https://eur-lex.europa.eu/legal-content/CS/TXT/?uri=CELEX:32024R2847

### SBOM formáty

- CycloneDX specification overview:  
  https://cyclonedx.org/specification/overview/
- CycloneDX 1.7 JSON reference:  
  https://cyclonedx.org/docs/1.7/json/
- SPDX specifications:  
  https://spdx.dev/use/specifications/

### Zephyr

- Zephyr `west spdx`:  
  https://docs.zephyrproject.org/latest/develop/west/zephyr-cmds.html#software-bill-of-materials-west-spdx
- Zephyr CRA guidance:  
  https://docs.zephyrproject.org/latest/security/standards/cyber-resilience-act.html

### PlatformIO

- Compilation database:  
  https://docs.platformio.org/en/latest/integration/compile_commands.html
- `pio pkg list`:  
  https://docs.platformio.org/en/latest/core/userguide/pkg/cmd_list.html
- `pio project metadata`:  
  https://docs.platformio.org/en/stable/core/userguide/project/cmd_metadata.html

### Mbed OS

- Arm Mbed OS EOL announcement:  
  https://www.arm.com/products/development-tools/embedded-and-software/mbed-os

### Vulnerability scanning

- Grype SBOM scanning:  
  https://oss.anchore.com/docs/guides/vulnerability/scan-targets/
- OSV-Scanner:  
  https://google.github.io/osv-scanner/

### Referenční projekt

- fw-context-mcp:  
  https://github.com/turbyho/fw-context-mcp

---

## Příloha A: minimální `.fw-context/components.toml`

```toml
schema = 1

[product]
name = "firmware"
supplier = "Company"
type = "firmware"

[[components]]
id = "app"
name = "firmware"
supplier = "Company"
type = "application"
paths = ["src/**", "include/**"]
version_source = "git"
git_root = "."

[[components]]
id = "framework"
name = "framework"
type = "framework"
paths = ["framework/**"]
version_source = "git"
git_root = "framework"
```

## Příloha B: minimální release příkaz

```bash
set -euo pipefail

fw-context index --build
fw-context sbom check
fw-context sbom generate \
  --release-version "$(git describe --tags --always)" \
  --output release/firmware.cdx.json

cyclonedx-cli validate \
  --input-file release/firmware.cdx.json \
  --fail-on-errors

grype release/firmware.cdx.json \
  --output json \
  > release/vulnerabilities.json

(
  cd release
  sha256sum * > SHA256SUMS
)
```

## Příloha C: doporučená definice hotového SBOM procesu

Proces je připraven pro produkční použití, pokud:

1. SBOM vzniká automaticky z produkčního buildu.
2. Každý release má vlastní archivovaný SBOM.
3. Každý aktivní build input má vlastníka.
4. Každá komponenta má jednoznačnou identitu.
5. Linker evidence je dostupná nebo je neúplnost výslovně deklarována.
6. Další firmware image a bloby jsou evidovány.
7. Vulnerability scan je oddělen od generování SBOM.
8. Kritické nálezy mají auditovatelné rozhodnutí.
9. VEX se opírá o build-aware důkazy.
10. Release artefakty, SBOM a rozhodnutí jsou neměnně archivovány.
