#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path.cwd().resolve()

TARGET_ROOTS = [
    Path("README.md"),
    Path("maps"),
    Path("wiki"),
    Path("users"),
]

SKIP_DIRS = {
    ".git",
    ".obsidian",
    "assets",
    "node_modules",
    "quartz",
}

GENERIC_HEADINGS = {
    "배경",
    "출발점",
    "동작 원리",
    "예시",
    "코드에서 보기",
    "코드 또는 값으로 보기",
    "값으로 보기",
    "숫자와 계산",
    "확인 방법",
    "이어서 볼 문서",
    "선택 기준",
    "확인할 것",
    "키워드",
    "질문",
}

MAX_PUBLIC_HEADING_LEN = 36

KNOWN_SUBJECTS = [
    (r"스레드 A가 CPU|스레드 전환", "컨텍스트 스위치"),
    (r"React나 브라우저에서 `?fetch\(\)`?", "fetch 요청 흐름"),
    (r"프로그램이 끝나지 않고 계속 실행", "타이머 선점"),
    (r"그렇다면 이 map|E820 메모리 맵", "E820 메모리 맵"),
    (r"`?timer_sleep", "timer_sleep 대기"),
    (r"`?write\s*`?에서 `?1`?|fd 1", "fd 1"),
    (r"같은 주소에 다시 `?mmap\(\)`?", "mmap 주소 재사용"),
    (r"지역 변수 배열", "스택 성장"),
    (r"유저 프로그램이 새 페이지", "프레임 교체"),
    (r"우선순위가 높은 스레드", "우선순위 기부"),
    (r"부모가 파일을 열어 둔 상태", "fd table fork 복제"),
    (r"언제 커널이 믿을 수 있는 struct", "syscall intr_frame"),
    (r"PintOS에서 다음 테스트", "argv 스택 테스트"),
    (r"그런데 PintOS VM 과제", "SPT 필요성"),
    (r"스택 page fault", "스택 성장 경계"),
    (r"가상 주소가 또 다른 가상 주소", "가상 주소 별칭"),
    (r"부모가 `?fork\(\)`?로 자식", "고아 프로세스"),
    (r"부모가 pid를 받는 순간", "fork 초기화 handshake"),
    (r"`?wait\s*`?를 호출", "wait/exit 동기화"),
    (r"Page Fault GDB에서 CR2", "CR2와 SPT 디버깅"),
    (r"GDB가 보여주는 `?RAX`?", "GDB register 해석"),
    (r"Transformer 블록 하나", "Transformer 블록 파라미터"),
    (r"QEMU는 Syscall 번호", "syscall 번호"),
    (r"QEMU는 프로세스 관리를 하지 않는다", "QEMU 프로세스 경계"),
    (r"바이트를 Struct Field", "Struct field 해석"),
    (r"바이트를 Pointer", "Pointer 해석"),
    (r"vm_do_claim_page", "vm_do_claim_page"),
    (r"CPU 레지스터와 명령어 실행", "CPU 실행"),
    (r"fork 때 SPT|SPT는 왜 같이 복사", "fork SPT 복사"),
    (r"fork에서 lazy UNINIT page", "fork lazy UNINIT"),
    (r"QEMU GDB Stub", "QEMU GDB Stub"),
    (r"process cleanup은 SPT page|process cleanup", "process cleanup"),
    (r"mmap file page의 aux", "mmap file aux 전환"),
    (r"fault가 한 번도 나지 않은 mmap page", "미사용 mmap page cleanup"),
    (r"mmap region metadata", "mmap region metadata"),
    (r"mmap 다중 region", "mmap 다중 region 격리"),
    (r"mmap은 왜 이미 쓰는 주소", "mmap 주소 겹침"),
    (r"mmap 실패 조건", "mmap 실패 조건"),
    (r"pml4_get_page는 왜 커널 가상 주소|pml4_get_page", "pml4_get_page"),
    (r"`read\(\)` 버퍼|read\(\) 버퍼", "read() 버퍼 PTE_W"),
    (r"fork는 왜 자식 초기화", "fork 초기화 handshake"),
    (r"swapped-out anonymous page|swapped anonymous page", "swapped anon fork"),
    (r"Supplemental Page Table", "SPT"),
    (r"Page Fault 뒤 user VA", "Page Fault 바이트 대응"),
    (r"Swap Slot 쓰기", "Swap Slot 쓰기"),
    (r"file-backed page metadata", "file-backed metadata"),
    (r"ELF 로더는 파일", "ELF 로더"),
    (r"페이지 폴트 후 왜 같은 명령어", "페이지 폴트 재실행"),
    (r"PintOS palloc과 User Frame 주소 별칭", "palloc frame 별칭"),
    (r"PintOS User Programs 실행 경계", "User-Kernel 경계"),
    (r"시스템 콜 진입의 스택 전환", "syscall 스택 전환"),
    (r"CPU 메모리 접근에서 Page Fault 복구", "Page Fault 복구"),
    (r"Disk sector는 QEMU block backend", "QEMU disk sector"),
    (r"E820 메모리 맵에서 palloc Pool", "E820 palloc pool"),
    (r"argv는 유저 스택", "argv 스택 배치"),
    (r"QEMU는 파일 시스템 정책", "QEMU 파일 시스템 경계"),
    (r"PintOS VM 구현 전", "PintOS VM 구현"),
    (r"lazy page aux cleanup", "lazy aux cleanup"),
    (r"lazy page aux ownership", "lazy aux 소유권"),
    (r"COW write-protect fault", "COW write fault"),
    (r"Clock hand로 victim frame", "Clock hand victim"),
    (r"Process Record는 왜 thread", "Process Record"),
    (r"fd table은 어떻게 복제", "fd table fork 복제"),
    (r"스택 성장 페이지 폴트", "스택 성장 폴트"),
    (r"QEMU 타이머 인터럽트", "QEMU 타이머 인터럽트"),
    (r"컨텍스트 스위치", "컨텍스트 스위치"),
    (r"웹 서버와 프록시 서버 실행 구조", "웹 서버와 프록시 실행"),
]

EXACT_VISIBLE_LABELS = GENERIC_HEADINGS | {
    "왜 필요한가",
    "왜 필요할까",
    "작은 질문",
    "작은 예시",
    "핵심 모델",
    "핵심 개념",
    "코드 증거",
    "코드 또는 값으로 보기",
    "직접 확인",
    "다음 링크",
    "관련 링크",
    "정리하며",
    "판단 기준",
    "체크포인트",
}

NATURAL_QUESTION_HEADINGS = {
    "꼬리 질문",
    "핵심 질문",
    "질문 사슬",
    "실제 판단 질문",
    "데이터 개발 질문",
    "읽을 때 확인할 질문",
    "다음에 넓힐 질문",
    "다음에 확인할 질문",
    "지원할 때 확인해야 할 질문",
    "도메인을 보는 8가지 질문",
    "구현 전 질문 은행",
    "GDB로 확인할 질문",
}


def markdown_files() -> list[Path]:
    files: list[Path] = []
    for root in TARGET_ROOTS:
        path = ROOT / root
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for item in path.rglob("*.md"):
                rel = item.relative_to(ROOT)
                if any(part in SKIP_DIRS for part in rel.parts):
                    continue
                if rel.parts[:2] == ("projects", "writing"):
                    continue
                files.append(item)
    return sorted(set(files))


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---", 4)
    if end == -1:
        return "", text
    return text[: end + 4], text[end + 5 :]


def frontmatter_value(text: str, key: str) -> str | None:
    frontmatter, _ = split_frontmatter(text)
    if not frontmatter:
        return None
    match = re.search(rf"^{re.escape(key)}:\s*(.+?)\s*$", frontmatter, re.M)
    if not match:
        return None
    value = match.group(1).strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]
    return value


def first_h1(text: str) -> str | None:
    _, body = split_frontmatter(text)
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def title_for(path: Path, text: str) -> str:
    return (
        frontmatter_value(text, "title")
        or first_h1(text)
        or path.stem.replace("-", " ")
    ).strip()


def clean_subject(title: str, path: Path) -> str:
    subject = re.sub(r"\s+", " ", title).strip()
    subject = re.sub(r"\s*\([^)]{2,80}\)\s*", " ", subject).strip()
    subject = re.split(r"\s+—\s+", subject, maxsplit=1)[0].strip()
    subject = re.split(r"\s*:\s+", subject, maxsplit=1)[0].strip()
    subject = re.sub(r"\b(Trace|Lab|Knowledge|Question|Guide)\b", "", subject).strip()
    subject = re.sub(r"(실습\s*)?재도전\s*가이드$", "실습", subject).strip()
    subject = re.sub(r"(실험|가이드|질문 은행과 체크리스트)$", "", subject).strip()
    if not subject:
        subject = path.stem.replace("-", " ")
    if len(subject) > 34:
        subject = subject[:34].rstrip() + "..."
    return subject


def fit_heading_text(text: str, limit: int = MAX_PUBLIC_HEADING_LEN) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("...", "").strip()
    if len(text) <= limit:
        return text
    parts = text.split()
    if len(parts) > 1:
        chosen: list[str] = []
        total = 0
        for part in parts:
            next_total = total + len(part) + (1 if chosen else 0)
            if next_total > limit:
                break
            chosen.append(part)
            total = next_total
        if chosen:
            return " ".join(chosen).rstrip("·,/ ")
    return text[:limit].rstrip("·,/ ")


def compact_subject(text: str, fallback: str) -> str:
    candidate = re.sub(r"\s+", " ", text).strip()
    candidate = candidate.strip(" ?")
    for pattern, replacement in KNOWN_SUBJECTS:
        if re.search(pattern, candidate, re.I):
            return replacement

    candidate = re.sub(r"\.{3,}", "", candidate).strip()
    candidate = re.sub(r"\s*\([^)]{2,80}\)\s*", " ", candidate).strip()
    candidate = re.sub(r"^요청 흐름에서 보이는\s+", "", candidate)
    candidate = re.sub(r"^실제 입력에서 달라지는\s+", "", candidate)
    candidate = re.sub(r"^한 번에 보는\s+(.+?)\s+사례$", r"\1", candidate)
    candidate = re.sub(r"^예로 보는\s+", "", candidate)
    candidate = re.sub(r"^숫자로 따라가는\s+", "", candidate)
    candidate = re.sub(r"^값으로 펼쳐 보는\s+", "", candidate)
    candidate = re.sub(r"^코드와 값으로 따라가는\s+", "", candidate)
    candidate = re.sub(r"^[^ ]+와 값으로 따라가는\s+", "", candidate)
    candidate = re.sub(r"^[0-9A-Fa-fx]+(?:/[0-9A-Fa-fx]+)? 값으로 따라가는\s+", "", candidate)
    candidate = re.sub(r"^(?:GDB|콘솔|요청과 로그)에서 확인할\s+", "", candidate)
    candidate = re.sub(r"^PintOS 코드에서 보이는\s+", "", candidate)
    candidate = re.sub(r"^텐서 코드에서 보이는\s+", "", candidate)
    candidate = re.sub(r"^(.+?)(?:은|는|이|가)\s*(?:왜|어떻게|언제|무엇|누가|어떤).*$", r"\1", candidate)
    candidate = re.sub(r"^(.+?)(?:을|를)\s*(?:왜|어떻게|언제|무엇|누가|어떤).*$", r"\1", candidate)
    candidate = re.sub(r"^(.+?)에서\s*(?:왜|어떻게|언제|무엇|누가|어떤).*$", r"\1", candidate)
    candidate = candidate.strip(" ?")
    if not candidate:
        candidate = fallback
    for pattern, replacement in KNOWN_SUBJECTS:
        if re.search(pattern, candidate, re.I):
            return replacement
    return fit_heading_text(candidate, 24)


def question_suffix(text: str) -> str:
    if "무슨 뜻" in text or "무엇일까" in text or "대체 무엇" in text:
        return "의미"
    if "왜" in text or "필요" in text:
        return "이유"
    if "어떻게" in text or "어떤 순서" in text or "흐름" in text:
        return "흐름"
    if "언제" in text or "조건" in text or "경계" in text:
        return "기준"
    return "질문"


def awkward_question_subject(text: str) -> bool:
    text = text.strip()
    if len(text) > 18:
        return True
    if text.endswith(("은", "는", "이", "가", "을", "를", "의", "와", "과", "에서", "하면", "이면", "려면", "뒤", "때")):
        return True
    return bool(
        re.search(
            r"(그렇다면|그런데|왜|어떻게|언제|무엇|무슨|어떤|누가|대체|인가|일까|"
            r"하면|이면|려면|으면|인데|뒤$|에서\s*$|`[^`]+`\s*[을를는은]?$)",
            text,
        )
    )


def compact_question_heading(text: str, fallback: str) -> str:
    subject = compact_subject(text, fallback)
    if awkward_question_subject(subject):
        subject = fallback
    suffix = question_suffix(text)
    if subject.endswith(("흐름", "의미", "이유", "기준", "경계", "전환", "복사", "점검", "디버깅", "테스트", "선점", "별칭", "필요성", "재사용")):
        return fit_heading_text(subject)
    return fit_heading_text(f"{subject} {suffix}")


def compact_generated_heading(title: str, subject: str, path: Path) -> str:
    original = title.strip()
    fallback = compact_subject(subject, path.stem.replace("-", " "))

    if original.endswith(" 질문") and original not in NATURAL_QUESTION_HEADINGS:
        raw_question_subject = original[:-3].strip()
        if awkward_question_subject(raw_question_subject):
            return compact_question_heading(raw_question_subject, fallback)
        return fit_heading_text(f"{compact_subject(raw_question_subject, fallback)} 쟁점")

    patterns = [
        (r"^(.+?) 준비 질문$", "{q}"),
        (r"^(.+?)의 시작점$", "{s} 시작"),
        (r"^(.+?)의 필요성이 드러나는 상황$", "{s} 필요성"),
        (r"^(.+?) 관련 문제 상황$", "{s} 문제"),
        (r"^(.+?) 관련 혼동$", "{s} 혼동"),
        (r"^(.+?)에서 구분해야 할 것$", "{s} 구분"),
        (r"^(.+?)에서 오해하기 쉬운 지점$", "{s} 오해"),
        (r"^(.+?)[을를] 가르는 기준(?:\s*\(.+?\))?$", "{s} 차이"),
        (r"^(.+?)의 작동 순서$", "{s} 흐름"),
        (r"^(.+?)의 작동 방식$", "{s} 구조"),
        (r"^(.+?)의 계산 방식$", "{s} 계산"),
        (r"^(.+?)의 시스템 상태 변화$", "{s} 상태"),
        (r"^(.+?) 상태$", "{s} 상태"),
        (r"^(.+?) 변화$", "{s} 변화"),
        (r"^(.+?) 값 추적$", "{s} 값 추적"),
        (r"^(.+?)의 최소 구조$", "{s} 구조"),
        (r"^(.+?) 안에서 실제로 바뀌는 것$", "{s} 변화"),
        (r"^실제 입력에서 달라지는 (.+?)$", "{s} 입력 차이"),
        (r"^한 번에 보는 (.+?) 사례$", "{s} 사례"),
        (r"^예로 보는 (.+?)$", "{s} 예시"),
        (r"^(.+?)에서 성공과 실패가 갈리는 장면$", "{s} 성공과 실패"),
        (r"^(.+?) 선택 기준$", "{s} 선택 기준"),
        (r"^(.+?) 사용이 어울리는 상황$", "{s} 사용 기준"),
        (r"^(.+?) 적용 선택지$", "{s} 적용 기준"),
        (r"^(.+?) 점검 질문$", "{s} 점검"),
        (r"^(.+?) 검증 지점$", "{s} 검증"),
        (r"^(.+?) 확인 순서$", "{s} 확인"),
        (r"^(.+?) 확인 기준$", "{s} 확인"),
        (r"^(.+?)에서 마지막으로 점검할 것$", "{s} 점검"),
        (r"^(.+?) 핵심 키워드$", "{s} 키워드"),
        (r"^(.+?)에서 따라갈 질문$", "{q}"),
        (r"^(.+?)의 코드 위치$", "{s} 코드"),
        (r"^PintOS 코드에서 보이는 (.+?)$", "{s} PintOS 코드"),
        (r"^텐서 코드에서 보이는 (.+?)$", "{s} 텐서 코드"),
        (r"^(.+?)에서 확인하는 (.+?)$", "{s} 코드"),
        (r"^(.+?)와 값으로 따라가는 (.+?)$", "{s} 코드와 값"),
        (r"^코드와 값으로 따라가는 (.+?)$", "{s} 코드와 값"),
        (r"^(.+?) 값으로 따라가는 (.+?)$", "{s} 값 추적"),
        (r"^숫자로 따라가는 (.+?)$", "{s} 숫자 추적"),
        (r"^값으로 펼쳐 보는 (.+?)$", "{s} 값 추적"),
        (r"^(.+?)에서 실제로 계산되는 값$", "{s} 값"),
        (r"^GDB에서 확인할 (.+?)$", "{s} GDB"),
        (r"^콘솔에서 확인할 (.+?)$", "{s} 콘솔"),
        (r"^요청과 로그로 확인하는 (.+?)$", "{s} 로그 확인"),
        (r"^(.+?)에서 이어지는 개념$", "{s} 관련 개념"),
        (r"^(.+?) 다음에 연결되는 문서$", "{s} 관련 문서"),
        (r"^(.+?)와 함께 읽을 문서$", "{s} 관련 문서"),
    ]

    for pattern, template in patterns:
        match = re.match(pattern, original)
        if match:
            raw_subject = match.group(match.lastindex or 1)
            if template == "{q}":
                return compact_question_heading(raw_subject, fallback)
            short = compact_subject(raw_subject, fallback)
            if template in {"{s} 상태", "{s} 변화"} and short.endswith(("해석", "경계", "흐름", "전환")):
                return fit_heading_text(short)
            return fit_heading_text(template.format(s=short))

    if len(original) > MAX_PUBLIC_HEADING_LEN and re.search(r"(왜|어떻게|언제|무엇|누가|어떤|까\??$)", original):
        return compact_question_heading(original, fallback)

    return fit_heading_text(original)


def stable_choice(path: Path, role: str, choices: list[str]) -> str:
    if not choices:
        return ""
    seed = f"{path.as_posix()}:{role}".encode()
    idx = int(hashlib.sha1(seed).hexdigest(), 16) % len(choices)
    return choices[idx]


def first_question(section: str) -> str | None:
    for raw in section.splitlines():
        line = raw.strip()
        if not line or line.startswith(("```", "~~~", "<!--", ">", "-", "|")):
            continue
        if "?" in line:
            line = line.split("?", 1)[0].strip()
            line = re.sub(r"^#+\s*", "", line)
            if 8 <= len(line) <= 64:
                return line
    return None


def first_sentence(section: str) -> str | None:
    for raw in section.splitlines():
        line = raw.strip()
        if not line or line.startswith(("```", "~~~", "<!--", ">", "-", "|", "#")):
            continue
        line = re.split(r"(?<=[.!?。])\s+", line, maxsplit=1)[0].strip()
        line = line.rstrip(".")
        if 8 <= len(line) <= 64:
            return line
    return None


def code_tokens(section: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"`([^`\n]{1,80})`", section):
        token = token.strip()
        if not token:
            continue
        if re.search(r"[A-Za-z_][A-Za-z0-9_]*\(\)$", token):
            tokens.append(token)
        elif "/" in token and len(token) <= 48:
            tokens.append(token.rsplit("/", 1)[-1])
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_.-]{2,40}$", token):
            tokens.append(token)
    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


def value_tokens(section: str) -> list[str]:
    patterns = [
        r"0x[0-9a-fA-F]+",
        r"\b\d+(?:KB|MB|GB|B|bit|byte|ms|s|개|원)\b",
        r"\b(?:RIP|RSP|CR2|CR3|PTE|PML4|TLB|fd|shape|logit|token|frame|sector)\b",
    ]
    found: list[str] = []
    for pattern in patterns:
        found.extend(re.findall(pattern, section))
    seen: set[str] = set()
    unique: list[str] = []
    for token in found:
        if token not in seen:
            seen.add(token)
            unique.append(token)
    return unique[:2]


def domain_for(path: Path) -> str:
    parts = path.relative_to(ROOT).parts
    if len(parts) >= 2 and parts[0] == "wiki":
        return parts[1]
    if parts[0] == "maps":
        return "maps"
    if parts[0] == "users":
        return "users"
    return "root"


def heading_from_role(path: Path, role: str, subject: str, section: str) -> str:
    domain = domain_for(path)
    question = first_question(section)
    sentence = first_sentence(section)
    codes = code_tokens(section)
    values = value_tokens(section)

    if role == "출발점":
        if question:
            return question
        return stable_choice(
            path,
            role,
            [
                f"{subject}에서 먼저 헷갈리는 지점",
                f"{subject}를 읽기 전에 잡을 질문",
                f"{subject}가 시작되는 문제",
            ],
        )

    if role == "배경":
        if domain in {"network", "backend"} and "요청" in section:
            return f"{subject}가 요청 흐름에서 보이는 자리"
        if "오해" in section or "헷갈" in section:
            return f"{subject}를 오해하기 쉬운 지점"
        if "느리" in section or "비용" in section:
            return f"{subject}가 비용을 줄이는 자리"
        return stable_choice(
            path,
            role,
            [
                f"{subject}가 문제로 드러나는 순간",
                f"{subject}를 구분해야 하는 이유",
                f"{subject}가 필요한 상황",
                f"{subject}를 그냥 넘기면 생기는 혼동",
            ],
        )

    if role == "동작 원리":
        if " vs " in subject or "와 " in subject or "과 " in subject:
            return f"{subject}를 가르는 기준"
        if "->" in section or "-->" in section or "sequenceDiagram" in section:
            return f"{subject}가 움직이는 순서"
        if domain == "ai" and ("확률" in section or "logit" in section or "token" in section):
            return f"{subject}가 계산되는 방식"
        if domain == "os" and ("레지스터" in section or "주소" in section or "페이지" in section):
            return f"{subject}가 시스템 상태로 바뀌는 과정"
        return stable_choice(
            path,
            role,
            [
                f"{subject}가 작동하는 방식",
                f"{subject}를 이해하는 최소 구조",
                f"{subject} 안에서 실제로 바뀌는 것",
            ],
        )

    if role == "예시":
        if "단순" in section and ("CoT" in section or "프롬프트" in section):
            return "단순 프롬프트와 단계별 프롬프트의 차이"
        if re.search(r"^###\s+[AB]\)", section, re.M) or "성공" in section and "실패" in section:
            return f"{subject}에서 성공과 실패가 갈리는 장면"
        if sentence and len(sentence) <= 36:
            return sentence
        return stable_choice(
            path,
            role,
            [
                f"{subject}를 갈라 보는 예",
                f"{subject}가 실제 입력에서 달라지는 장면",
                f"{subject}를 한 번에 보는 작은 사례",
            ],
        )

    if role == "코드에서 보기":
        if codes:
            return f"{codes[0]}에서 확인하는 {subject}"
        if domain == "ai":
            return f"{subject}가 텐서 코드에 드러나는 자리"
        if domain == "os":
            return f"{subject}가 PintOS 코드에 나타나는 자리"
        return f"{subject}가 코드에 나타나는 자리"

    if role == "코드 또는 값으로 보기":
        if codes:
            return f"{codes[0]}와 값으로 따라가는 {subject}"
        return f"코드와 값으로 따라가는 {subject}"

    if role in {"값으로 보기", "숫자와 계산"}:
        if values:
            joined = "/".join(values)
            return f"{joined} 값으로 따라가는 {subject}"
        if "shape" in section:
            return f"shape로 따라가는 {subject}"
        return stable_choice(
            path,
            role,
            [
                f"{subject}를 숫자로 따라가기",
                f"{subject}에서 실제로 계산되는 값",
                f"{subject}를 값 단위로 펼쳐 보기",
            ],
        )

    if role == "확인 방법":
        lower = section.lower()
        if "gdb" in lower:
            return f"GDB에서 확인할 {subject}"
        if "curl" in lower or "fetch" in lower or "로그" in section:
            return f"요청과 로그로 확인하는 {subject}"
        if "aws" in lower or "콘솔" in section:
            return f"콘솔에서 확인할 {subject}"
        return stable_choice(
            path,
            role,
            [
                f"{subject}를 검증할 때 볼 지점",
                f"{subject}가 맞는지 확인하는 방법",
                f"{subject}를 직접 확인할 순서",
            ],
        )

    if role == "이어서 볼 문서":
        return stable_choice(
            path,
            role,
            [
                f"{subject}에서 이어지는 개념",
                f"{subject} 다음에 연결되는 문서",
                f"{subject}와 함께 읽을 문서",
            ],
        )

    if role == "선택 기준":
        if "어울리는 경우" in section or "과한 경우" in section:
            return f"언제 {subject}를 쓸지"
        return stable_choice(
            path,
            role,
            [
                f"{subject}를 고르는 기준",
                f"{subject}에서 판단이 갈리는 지점",
                f"{subject}를 적용할 때의 선택지",
            ],
        )

    if role == "확인할 것":
        return stable_choice(
            path,
            role,
            [
                f"{subject}를 읽고 남길 기준",
                f"{subject}에서 마지막으로 점검할 것",
                f"{subject}가 이해됐는지 보는 질문",
            ],
        )

    if role == "키워드":
        return f"{subject}를 이루는 키워드"

    if role == "질문":
        return f"{subject}에서 따라갈 질문"

    return role


def collect_sections(lines: list[str]) -> dict[int, str]:
    sections: dict[int, str] = {}
    heading_positions: list[tuple[int, int, str]] = []
    in_fence = False
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{2,4})\s+(.+?)\s*$", line.rstrip("\n"))
        if match:
            heading_positions.append((idx, len(match.group(1)), match.group(2).strip()))

    for pos, (idx, level, _title) in enumerate(heading_positions):
        end = len(lines)
        for next_idx, next_level, _ in heading_positions[pos + 1 :]:
            if next_level <= level:
                end = next_idx
                break
        sections[idx] = "".join(lines[idx + 1 : end])
    return sections


def unique_heading(candidate: str, used: set[str], role: str) -> str:
    candidate = re.sub(r"\s+", " ", candidate).strip()
    candidate = candidate.rstrip(".")
    if len(candidate) > MAX_PUBLIC_HEADING_LEN:
        candidate = fit_heading_text(candidate)
    if candidate not in used:
        used.add(candidate)
        return candidate
    fallback = f"{candidate} ({role})"
    used.add(fallback)
    return fallback


def polish_generated_heading(title: str) -> str:
    replacements = [
        (r"^(.+?)[을를] 읽기 전에 잡을 질문$", r"\1 준비 질문"),
        (r"^(.+?)[이가] 시작되는 문제$", r"\1의 시작점"),
        (r"^(.+?)[이가] 필요한 상황$", r"\1의 필요성이 드러나는 상황"),
        (r"^(.+?)[이가] 문제로 드러나는 순간$", r"\1 관련 문제 상황"),
        (r"^(.+?)[이가] 요청 흐름에서 보이는 자리$", r"요청 흐름에서 보이는 \1"),
        (r"^(.+?)[을를] 그냥 넘기면 생기는 혼동$", r"\1 관련 혼동"),
        (r"^(.+?)[을를] 구분해야 하는 이유$", r"\1에서 구분해야 할 것"),
        (r"^(.+?)[을를] 오해하기 쉬운 지점$", r"\1에서 오해하기 쉬운 지점"),
        (r"^(.+?)[이가] 움직이는 순서$", r"\1의 작동 순서"),
        (r"^(.+?)[이가] 작동하는 방식$", r"\1의 작동 방식"),
        (r"^(.+?)[이가] 계산되는 방식$", r"\1의 계산 방식"),
        (r"^(.+?)[이가] 시스템 상태로 바뀌는 과정$", r"\1의 시스템 상태 변화"),
        (r"^(.+?)[을를] 이해하는 최소 구조$", r"\1의 최소 구조"),
        (r"^(.+?)[을를] 갈라 보는 예$", r"예로 보는 \1"),
        (r"^(.+?)[이가] 실제 입력에서 달라지는 장면$", r"실제 입력에서 달라지는 \1"),
        (r"^(.+?)[을를] 한 번에 보는 작은 사례$", r"한 번에 보는 \1 사례"),
        (r"^(.+?)[을를] 고르는 기준$", r"\1 선택 기준"),
        (r"^언제 (.+?)[을를] 쓸지$", r"\1 사용이 어울리는 상황"),
        (r"^(.+?)[을를] 적용할 때의 선택지$", r"\1 적용 선택지"),
        (r"^(.+?)[을를] 읽고 남길 기준$", r"\1 점검 질문"),
        (r"^(.+?)[을를] 검증할 때 볼 지점$", r"\1 검증 지점"),
        (r"^(.+?)[을를] 직접 확인할 순서$", r"\1 확인 순서"),
        (r"^(.+?)[이가] 맞는지 확인하는 방법$", r"\1 확인 기준"),
        (r"^(.+?)[이가] 이해됐는지 보는 질문$", r"\1 점검 질문"),
        (r"^(.+?)[을를] 숫자로 따라가기$", r"숫자로 따라가는 \1"),
        (r"^(.+?)[을를] 값 단위로 펼쳐 보기$", r"값으로 펼쳐 보는 \1"),
        (r"^(.+?)[이가] 코드에 나타나는 자리$", r"\1의 코드 위치"),
        (r"^(.+?)[이가] PintOS 코드에 나타나는 자리$", r"PintOS 코드에서 보이는 \1"),
        (r"^(.+?)[이가] 텐서 코드에 드러나는 자리$", r"텐서 코드에서 보이는 \1"),
        (r"^(.+?)[을를] 이루는 키워드$", r"\1 핵심 키워드"),
    ]
    polished = title
    for pattern, replacement in replacements:
        polished = re.sub(pattern, replacement, polished)
    polished = polished.replace("프롬프팅를", "프롬프팅을")
    polished = polished.replace("튜닝를", "튜닝을")
    polished = polished.replace("검색를", "검색을")
    polished = polished.replace("정렬를", "정렬을")
    polished = polished.replace("손실를", "손실을")
    polished = polished.replace("모델를", "모델을")
    polished = polished.replace("실습를", "실습을")
    polished = polished.replace("구현를", "구현을")
    polished = polished.replace("함수를", "함수를")
    return polished


def same_question_heading(heading: str, line: str) -> bool:
    left = heading.strip().rstrip("?")
    right = line.strip().rstrip("?")
    return bool(left) and left == right


def remove_duplicated_opening_lines(lines: list[str]) -> tuple[list[str], int]:
    remove: set[int] = set()
    for idx, line in enumerate(lines[:-1]):
        match = re.match(r"^(#{2,4})\s+(.+?)\s*$", line.rstrip("\n"))
        if not match:
            continue
        heading = match.group(2).strip()
        probe = idx + 1
        while probe < len(lines) and not lines[probe].strip():
            probe += 1
        if probe >= len(lines):
            continue
        if same_question_heading(heading, lines[probe]):
            remove.add(probe)
            if probe + 1 < len(lines) and not lines[probe + 1].strip():
                remove.add(probe + 1)
    if not remove:
        return lines, 0
    return [line for idx, line in enumerate(lines) if idx not in remove], len(remove)


def rewrite_file(path: Path, text: str) -> tuple[str, dict[str, str], int]:
    title = title_for(path, text)
    subject = clean_subject(title, path)
    lines = text.splitlines(keepends=True)
    sections = collect_sections(lines)
    used = {
        match.group(2).strip()
        for line in lines
        if (match := re.match(r"^(#{2,4})\s+(.+?)\s*$", line.rstrip("\n")))
        and match.group(2).strip() not in GENERIC_HEADINGS
    }
    anchor_map: dict[str, str] = {}
    changed = 0
    in_fence = False

    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{2,4})\s+(.+?)(\s*)$", line.rstrip("\n"))
        if not match:
            continue
        role = match.group(2).strip()
        if role in GENERIC_HEADINGS:
            section = sections.get(idx, "")
            candidate = heading_from_role(path, role, subject, section)
        else:
            candidate = role
        candidate = polish_generated_heading(candidate)
        candidate = compact_generated_heading(candidate, subject, path)
        if candidate == role:
            continue
        new_heading = unique_heading(candidate, used, role)
        newline = "\n" if line.endswith("\n") else ""
        lines[idx] = f"{match.group(1)} {new_heading}{newline}"
        anchor_map[role] = new_heading
        changed += 1

    lines, removed = remove_duplicated_opening_lines(lines)
    changed += removed

    return "".join(lines), anchor_map, changed


def link_keys(path: Path) -> set[str]:
    rel = path.relative_to(ROOT).with_suffix("").as_posix()
    keys = {rel, path.stem}
    if path.name == "README.md":
        if path.parent == ROOT:
            keys.update({"README", "index"})
        else:
            keys.add(path.parent.name)
            keys.add(rel.removesuffix("/README"))
    return {key for key in keys if key}


def build_index(files: list[Path]) -> dict[str, Path]:
    raw: dict[str, list[Path]] = {}
    for path in files:
        for key in link_keys(path):
            raw.setdefault(key, []).append(path)
    return {key: paths[0] for key, paths in raw.items() if len(paths) == 1}


WIKILINK_RE = re.compile(r"\[\[([^\]|#]*)(?:#([^\]|]+))?(\|[^\]]*)?\]\]")
BAD_ALIAS_RE = re.compile(
    r"(를 이루는 키워드|를 읽기 전에 잡을 질문|가 필요한 상황|가 움직이는 순서|"
    r"를 구분해야 하는 이유|를 갈라 보는 예|를 읽고 남길 기준|를 고르는 기준|"
    r"필요성이 드러나는 상황|관련 문제 상황|관련 혼동|작동 순서|작동 방식|"
    r"계산 방식|시스템 상태 변화|구분해야 할 것|오해하기 쉬운 지점|"
    r"사용이 어울리는 상황|점검 질문|검증 지점|확인 순서|확인 기준|"
    r"코드 위치|핵심 키워드|준비 질문|에서 이어지는 개념|다음에 연결되는 문서|"
    r"와 함께 읽을 문서|값으로 따라가는|GDB에서 확인할|코드에서 확인하는|"
    r"프롬프팅를|튜닝를|실습를|정렬를|검색를)"
)


def rewrite_links(text: str, current: Path, index: dict[str, Path], maps: dict[Path, dict[str, str]]) -> tuple[str, int]:
    changed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        target = match.group(1)
        anchor = match.group(2)
        alias = match.group(3)
        if not anchor:
            return match.group(0)
        resolved = current if not target else index.get(target.removesuffix(".md"))
        if not resolved:
            return match.group(0)
        anchor_map = maps.get(resolved, {})
        new_anchor = anchor_map.get(anchor)
        alias_text = alias[1:] if alias else ""
        alias_needs_polish = bool(alias_text and BAD_ALIAS_RE.search(alias_text))
        if not new_anchor and not alias_needs_polish:
            return match.group(0)
        final_anchor = new_anchor or anchor
        new_alias = alias
        if alias and (alias[1:] in EXACT_VISIBLE_LABELS or alias_needs_polish):
            new_alias = "|" + final_anchor
        changed += 1
        return f"[[{target}#{final_anchor}{new_alias or ''}]]"

    updated = WIKILINK_RE.sub(replace, text)
    return updated, changed


def main() -> None:
    files = markdown_files()
    texts = {path: path.read_text(encoding="utf-8", errors="ignore") for path in files}
    anchor_maps: dict[Path, dict[str, str]] = {}
    updated_texts: dict[Path, str] = {}
    heading_changes = 0

    for path, text in texts.items():
        updated, anchor_map, changes = rewrite_file(path, text)
        updated_texts[path] = updated
        if anchor_map:
            anchor_maps[path] = anchor_map
        heading_changes += changes

    index = build_index(files)
    link_changes = 0
    files_changed = 0
    for path in files:
        updated, changes = rewrite_links(updated_texts[path], path, index, anchor_maps)
        link_changes += changes
        if updated != texts[path]:
            path.write_text(updated, encoding="utf-8")
            files_changed += 1

    print(f"personalized_heading_files_changed={files_changed}")
    print(f"personalized_heading_changes={heading_changes}")
    print(f"personalized_anchor_link_changes={link_changes}")


if __name__ == "__main__":
    main()
