<div align="center">

# Aura Studio 1 Auto-Off Patch

> Harman Kardon Aura Studio 1의 자동 전원 꺼짐을 막는 펌웨어 덤프·분석·1바이트 패치.

[English](README.md) | 한국어

</div>

## 문제와 해결

Aura Studio 1은 소리가 없는 상태가 이어지면 스스로 전원을 끄고, 이를 끄는 설정이 없다.
이 펌웨어 패치는 자동 전원 끄기 이벤트를 예약하는 부분만 막는다. 대기 진입, 대기 복귀, 전원 버튼 종료는 그대로다.

대상: Aura Studio 1, FCC ID `APIHKAURAST`, USB `0ECB:1EB2`. 1세대 Aura(`HK AURA`)와 Aura Wi-Fi 계열은 대상이 아니다.

## 포함된 것

- USB HID DFU로 두 번 읽어 바이트 단위로 일치한 전체 펌웨어 (`firmware_bin/`)
- CSR-dfu2 컨테이너를 영역, 내부 파일 17개, PS record 58개로 분리한 결과 (`firmware_analysis/`)
- Stack 프로그램과 VM 애플리케이션의 XAP 어셈블리 (`firmware_analysis/disassembly/`)
- 자동 종료 제어 흐름 추적과 근거 발췌 (`firmware_analysis/power_off/`)
- 수정본·복구용 DFU 패키지와 실제 기록 로그 전체 (`firmware_patched/`)
- 읽기·패치·기록 도구(`tools/`)와 오프라인 테스트 24개(`tests/`)

## 동작 방식

1. **추적.** VM 애플리케이션에서 대기 이벤트 `0x60D8`이 타이머 `0x60DF`를 예약하고, 그 처리기가 전원 끄기 이벤트 `0x6002`를 보낸다.
2. **패치.** VM code word `0xE0F`의 분기 `BEQ 0x1C6E`를 `BRA 0x1C6E`로 바꿔 `0x60DF`가 예약되지 않게 한다.
   DFU 오프셋 `0xDFCE6`의 1바이트(`F4`→`E0`)다.
3. **검사값.** VM 헤더 XOR16(1바이트)과 파일시스템 헤더 CRC32(4바이트)를 갱신하고, 파일 끝 CRC는 그대로다.
   파일시스템 preamble이 전부 `FF`로 읽힌 덤프에는 기기가 요구하는 정식 `fsr_dfu1` preamble을 넣는다.
4. **자기 기기에서 생성.** 기록 도구는 스피커를 읽고, 그 재읽기로 대상 이미지를 만들며 그 기기의 PS(설정) 영역을 바이트 단위로 유지한다.
5. **펌웨어 확인.** 여기서 분석한 펌웨어 빌드만 받는다. 지문에서 PS 영역을 빼므로 같은 빌드의 기기는 모두 통과하고 다른 빌드는 거부된다.
6. **안전한 기록.** 쓴 뒤 전체를 다시 읽어 바이트 단위로 같을 때만 reset을 보낸다.

다른 원인으로 생기는 종료는 건드리지 않는다.

## 상태

- 2026-11-07 실기 기록 완료: 최종 status 0, 전체 재읽기가 수정 이미지와 일치 (`firmware_patched/flashing/run_20261107T081225Z/`).
- 이후 대기 상태로 몇 시간 동안 꺼지지 않았고 재생도 정상이다.

## 요구 사항

- macOS(`ctypes`로 IOKit 사용), Python 3 표준 라이브러리만 사용
- 스피커를 Mac 본체 USB-C 포트에 직접 연결
- 업데이트 모드: **Volume− + Phone**을 5초 누른다. Volume− + Bluetooth는 데모 모드이므로 쓰지 않는다.

## 구성

```
firmware_bin/
  aura_studio1_original.bin          첫 번째 덤프 (SHA-256 d287d29d…)
  aura_studio1_repeat.bin            두 번째 덤프, 동일
firmware_analysis/
  regions/  stack/  filesystem/fs00/ 분리한 컨테이너
  settings.json  settings.md         PS record
  disassembly/stack_program.asm      Stack 프로그램 어셈블리
  disassembly/vm_program.asm         VM 애플리케이션 어셈블리
  power_off/                         자동 종료 추적
firmware_patched/                    원본 기기의 작업 이력
  device_package/                    실제로 기록한 이미지와 그 직전 이미지
  usb_package_v2/                    FS preamble을 처음 재구성한 이미지
  usb_package/, 루트 *.dfu           첫 시도본. 기기가 거부했다. 기록 금지
  flashing/run_*/                    기록 로그와 재읽기
  recovery/  preflight/              FS preamble 재구성 증거와 기록 전 점검
tools/
  device_read.py                     읽기 전용 덤프
  device_write.py                    스피커에 patch / unpatch 기록
  device_usb_config.c                USB 구성 1 선택
  image_patch.py                     덤프에서 patch / unpatch 이미지 생성 (오프라인)
  image_unpack.py                    위 두 도구가 쓰는 CSR-dfu2 컨테이너 해석기
tests/                               오프라인 테스트 (python3 -m unittest)
runs/                                직접 실행한 기록 로그 (git 제외)
```

## 사용법

저장소 루트에서 실행한다. 먼저 스피커를 업데이트 모드로 둔다.

```sh
# 0. 테스트 (오프라인)
python3 -m unittest

# 1. 읽기 전용 덤프 -> my_dump/firmware.upload
python3 tools/device_read.py --dump-dir my_dump --log my_dump.jsonl

# 2. 오프라인 계획: 펌웨어 확인, 바뀌는 바이트, 전송 분할. 기기 접근 없음
python3 tools/device_write.py patch --dump my_dump/firmware.upload

# 3. 기록. 스피커가 검토한 덤프와 달라졌으면 거부한다
python3 tools/device_write.py patch --dump my_dump/firmware.upload --apply
```

되돌리려면 같은 순서로 `patch` 대신 `unpatch`를 쓴다.
기록할 때마다 `runs/run_<UTC 시각>/`에 `pre_write.upload`, `target.dfu`, `post_write.upload`와 로그가 남는다.
`tools/image_patch.py patch my_dump/firmware.upload --output out.dfu`는 기기 없이 이미지 파일만 만든다.

업데이트 모드에서 USB configuration이 0이라 HID가 보이지 않으면 먼저 구성 1을 선택한다.

```sh
clang -framework IOKit -framework CoreFoundation tools/device_usb_config.c -o tools/device_usb_config
tools/device_usb_config --activate
```

`Unsupported firmware build`가 나오면 그 기기의 펌웨어 빌드가 여기서 분석한 것과 다르다는 뜻이며, 아무것도 기록하지 않는다.

## 펌웨어와 면책

비공식 프로젝트이며 Harman International과 관계가 없다.
펌웨어 이미지와 어셈블리는 Harman의 저작물이며, 단종 제품의 상호운용과 보존을 위해 공개한다.
펌웨어 기록은 스피커를 못 쓰게 만들 수 있다. 책임은 사용자에게 있다.

## 라이선스

직접 작성한 도구와 문서는 [MIT](LICENSE).
펌웨어 이미지와 그 파생 파일(`firmware_bin/`, `firmware_analysis/`, `firmware_patched/`)은 적용 대상이 아니다.
