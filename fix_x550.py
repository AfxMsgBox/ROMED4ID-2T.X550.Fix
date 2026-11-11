#!/usr/bin/env python3
"""ROMED4ID-2T 的已验证 X550 Port0 MDI 配置修复。先阅读 README.md。"""

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

BASE = Path(__file__).resolve().parent
CONFIG = Path(__file__).resolve().with_suffix('.conf')
START, OFFSET, END = 0xFC2 * 2, 0xFC5 * 2, 0xFCC * 2
OLD = struct.pack('<10H', 9, 0xE400, 1, 0, 1, 0xE400, 0x21, 1, 1, 0x6000)
NEW = struct.pack('<10H', 9, 0xE400, 1, 1, 1, 0xE400, 0x21, 1, 1, 0x3D00)
IDS = {'vendor': '0x8086', 'device': '0x1563',
       'subsystem_vendor': '0x1849', 'subsystem_device': '0x1563'}
DEFAULTS = {'action': 'check', 'interface': '', 'backup_dir': str(BASE / 'backups'),
            'restore_dir': ''}


def crc8(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ (7 if value & 128 else 0)) & 255
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def candidate(before):
    if len(before) != 16384:
        raise ValueError('要求完整的 16KB EEPROM 读回')
    if struct.unpack_from('<H', before, 0x43 * 2)[0] != 0xFC2:
        raise ValueError('PHY 配置指针不匹配，不支持此布局')
    section = before[START:END]
    if section not in (OLD, NEW) or crc8(section[:-2]) != section[-1]:
        raise ValueError('配置模板或 CRC8 不匹配，不支持此故障状态')
    after = bytearray(before)
    after[START:END] = NEW
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    if changed not in ([], [8074, 8087]):
        raise ValueError('候选出现预期之外的修改')
    return bytes(after)


def run(args, *, data=None, check=True):
    p = subprocess.run(args, input=data, capture_output=True, timeout=90)
    if check and p.returncode:
        raise RuntimeError(f'{args[0]} 失败：{p.stderr.decode(errors="replace").strip()}')
    return p


def read_eeprom(interface):
    return run(['ethtool', '-e', interface, 'raw', 'on']).stdout


def write_eeprom(interface, image):
    if len(image) != 16384:
        raise ValueError('禁止以非完整校验镜像生成写入负载')
    return run(['ethtool', '-E', interface, 'magic', '0x15638086',
                'offset', str(OFFSET), 'length', str(END - OFFSET)],
               data=image[OFFSET:END], check=False)


def fields(text):
    return dict(line.split(':', 1) for line in text.splitlines() if ':' in line)


def inspect_device(interface):
    if Path('/sys/class/dmi/id/board_name').read_text().strip() != 'ROMED4ID-2T':
        raise ValueError('主板不是 ROMED4ID-2T')
    root = Path('/sys/class/net')
    def identity(dev):
        return all((dev / k).read_text().strip().lower() == v for k, v in IDS.items())
    if not interface:
        choices = []
        for p in root.iterdir():
            dev = p / 'device'
            if dev.exists() and dev.resolve().name.endswith('.0'):
                try:
                    if identity(dev):
                        choices.append(p.name)
                except FileNotFoundError:
                    pass
        if len(choices) != 1:
            raise ValueError('不能唯一识别目标 Port0，请用 --interface 指定')
        interface = choices[0]
    if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', interface):
        raise ValueError('接口名称不合法')
    dev = root / interface / 'device'
    pci = dev.resolve().name
    if not pci.endswith('.0') or not identity(dev):
        raise ValueError('必须指定 8086:1563 / 1849:1563 的 Port0（PCI function 0）')
    peers = [p.name for p in root.iterdir()
             if (p / 'device').exists() and (p / 'device').resolve().name == pci[:-1] + '1']
    if len(peers) != 1 or not identity(root / peers[0] / 'device'):
        raise ValueError('同一 X550 芯片的 Port1 不存在或标识不符')
    info = {'interface': interface, 'peer': peers[0], 'pci': pci,
            'macs': {}, 'drivers': {}}
    for name in (interface, peers[0]):
        p = root / name
        driver_text = run(['ethtool', '-i', name]).stdout.decode()
        driver = {k: v.strip() for k, v in fields(driver_text).items()}
        if driver.get('driver') != 'ixgbe' or not re.search(r'\b(?:0x)?8000172d\b', driver.get('firmware-version', ''), re.I):
            raise ValueError('仅支持 ixgbe 和已验证 EEPID 8000172D；不自动升级或降级固件')
        info['macs'][name] = (p / 'address').read_text().strip()
        info['drivers'][name] = driver
    return info


def assert_idle(info):
    """拒绝影响正在承载业务的两口；管理 UP 本身不等于承载业务。"""
    links = json.loads(run(['ip', '-j', '-d', 'link']).stdout)
    names = {info['interface'], info['peer']}
    indexes = {x['ifindex'] for x in links if x['ifname'] in names}
    for link in links:
        if link['ifname'] in names and link.get('master'):
            raise ValueError('X550 接口属于网桥或 bond，请先在维护窗口停用业务')
        if link.get('link_index') in indexes or link.get('link') in names:
            raise ValueError('X550 存在 VLAN/macvlan 等上层接口，拒绝写入')
    for name in names:
        p = Path('/sys/class/net') / name
        if list(p.glob('upper_*')) or list((p / 'device').glob('virtfn*')):
            raise ValueError('X550 存在上层接口或 SR-IOV VF，拒绝写入')
        addrs = json.loads(run(['ip', '-j', 'addr', 'show', 'dev', name]).stdout)
        if any(a['scope'] != 'link' for row in addrs for a in row['addr_info']):
            raise ValueError('X550 存在非链路本地地址，拒绝写入')
        for family in ('-4', '-6'):
            routes = json.loads(run(['ip', family, '-j', 'route', 'show', 'table', 'all', 'dev', name]).stdout)
            if any(not r.get('dst', '').startswith(('fe80:', 'ff00:')) for r in routes):
                raise ValueError('X550 存在业务路由，拒绝写入')


def save_file(path, data):
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def backup(directory, info, before, after, action):
    base = Path(directory).expanduser()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=datetime.now().strftime('%Y%m%d-%H%M%S-'), dir=base))
    meta = {'format': 1, 'action': action, 'board': 'ROMED4ID-2T', **info,
            'time': datetime.now().astimezone().isoformat(), 'before_sha256': sha(before),
            'candidate_sha256': sha(after), 'offset': OFFSET, 'length': END - OFFSET,
            'backup_scope': '16KB EEPROM readback, not a full 2MB flash image'}
    save_file(folder / 'before.bin', before)
    save_file(folder / 'candidate.bin', after)
    save_file(folder / 'metadata.json', json.dumps(meta, indent=2).encode())
    sync_dir(folder)
    sync_dir(base)
    # 落盘后读回文件，再允许接触硬件。
    if (folder / 'before.bin').read_bytes() != before or (folder / 'candidate.bin').read_bytes() != after:
        raise RuntimeError('备份文件读回不一致')
    return folder


def rollback_target(folder, info, current):
    folder = Path(folder).expanduser()
    meta = json.loads((folder / 'metadata.json').read_text())
    before, after = (folder / 'before.bin').read_bytes(), (folder / 'candidate.bin').read_bytes()
    if (meta.get('format'), meta.get('action'), meta.get('board')) != (1, 'apply', 'ROMED4ID-2T'):
        raise ValueError('不是此脚本生成的修复备份')
    if meta.get('pci') != info['pci'] or meta.get('macs') != info['macs']:
        raise ValueError('备份不属于当前设备/接口；禁止跨机器回退')
    if sha(before) != meta.get('before_sha256') or sha(after) != meta.get('candidate_sha256'):
        raise ValueError('备份哈希不符')
    if before == after or candidate(before) != after or current != after:
        raise ValueError('当前 EEPROM 不等于这次修复的候选；拒绝覆盖后续更改')
    return before


def transact(info, before, after, backup_dir, action):
    assert_idle(info)
    folder = backup(backup_dir, info, before, after, action)
    print(f'备份：{folder}（16KB 读回，不是完整 2MB Flash）', flush=True)
    # 以最后一次实际设备读回阻止备份过程中发生的状态变化。
    for name in (info['interface'], info['peer']):
        if read_eeprom(name) != before:
            raise RuntimeError('备份期间设备内容变化，未写入')
    result = {'action': action, 'persistence': 'pending AC power cycle'}
    try:
        p = write_eeprom(info['interface'], after)
        result.update(returncode=p.returncode, stdout=p.stdout.decode(errors='replace'),
                      stderr=p.stderr.decode(errors='replace'))
        matched = True
        for index, name in enumerate((info['interface'], info['peer'])):
            readback = read_eeprom(name)
            save_file(folder / f'after-port{index}.bin', readback)
            matched = matched and readback == after
        result['readback_matches'] = matched
        if p.returncode or not matched:
            raise RuntimeError('写入或读回异常；停止，保留现场，禁止连续盲写。备份目录见上方')
    except BaseException as error:
        result['error'] = str(error)
        raise
    finally:
        save_file(folder / 'result.json', json.dumps(result, indent=2).encode())
        sync_dir(folder)
    print('最小写入和两口完整读回通过。尚需正常关机、完全断电、重新启动后执行 check。')
    print('本脚本不自动断电、重启、修改 PHY 运行寄存器或配置网络。')


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='优先级：交互输入 > 命令行 > 显式配置 > 默认值。\n无参数进入交互；有参数不交互。--yes 不从配置读取、不保存。')
    g = p.add_argument_group('操作与设备')
    g.add_argument('--action', choices=('check', 'apply', 'rollback'), help='默认 check，只读；apply 修复；rollback 回退')
    g.add_argument('--interface', help='目标 Port0 接口；默认自动识别唯一匹配设备，不固定 eth2')
    g.add_argument('--yes', action='store_true', help='非交互 apply/rollback 必填：明确批准最小 EEPROM 写入')
    g = p.add_argument_group('备份与回退')
    g.add_argument('--backup-dir', help=f'写入前备份父目录；默认 {BASE / "backups"}')
    g.add_argument('--restore-dir', help='rollback 必填：本脚本生成的某次 apply 备份子目录')
    g = p.add_argument_group('配置')
    g.add_argument('--config', nargs='?', const=str(CONFIG), help=f'显式读取 JSON 配置；省略 PATH 时为 {CONFIG}；默认不读取')
    return p


def options(argv):
    ns = parser().parse_args(argv)
    values = dict(DEFAULTS)
    if ns.config:
        config = json.loads(Path(ns.config).expanduser().read_text())
        if not isinstance(config, dict) or set(config) - set(DEFAULTS):
            raise ValueError('配置仅允许 action/interface/backup_dir/restore_dir，不允许 yes')
        if any(not isinstance(v, str) for v in config.values()):
            raise ValueError('配置值必须为字符串')
        values.update(config)
    values.update({k: v for k, v in vars(ns).items() if k in DEFAULTS and v is not None})
    if not argv:
        if not sys.stdin.isatty():
            raise ValueError('无参数需要交互终端；自动化请使用 --action check 或明确的操作参数')
        for key, label in [('action', '操作 check/apply/rollback'), ('interface', 'Port0 接口（留空自动识别）'),
                           ('backup_dir', '备份父目录'), ('restore_dir', '回退备份目录（仅 rollback）')]:
            if key == 'restore_dir' and values['action'] != 'rollback':
                continue
            values[key] = input(f'{label} [{values[key]}]: ').strip() or values[key]
    if values['action'] not in ('check', 'apply', 'rollback'):
        raise ValueError('未知 action')
    if not values['backup_dir'] or (values['action'] == 'rollback' and not values['restore_dir']):
        raise ValueError('backup-dir 不能为空，rollback 必须提供 restore-dir')
    if argv and values['action'] != 'check' and not ns.yes:
        raise ValueError('有参数时不交互；写入操作必须明确提供 --yes')
    return values


def save_config(values):
    if input(f'保存配置到 {CONFIG}？不保存写入授权 [y/N]: ').strip().lower() != 'y':
        return
    if CONFIG.exists() or CONFIG.is_symlink():
        if input('配置已存在，覆盖？[y/N]: ').strip().lower() != 'y':
            return
    # 原子替换而非跟随现有符号链接写入；临时文件及最终文件均 0600。
    fd, name = tempfile.mkstemp(prefix='.x550-config-', dir=CONFIG.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(values, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, CONFIG)
        sync_dir(CONFIG.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    values = options(argv)
    if os.geteuid() != 0:
        raise ValueError('请以 root 运行（check 读取 EEPROM 也需要权限）')
    for tool in ('ethtool', 'ip'):
        if not shutil.which(tool):
            raise ValueError(f'缺少 {tool}；请先安装 python3 ethtool iproute2')
    os.umask(0o077)
    info = inspect_device(values['interface'])
    # flock 使用已存在的 sysfs 目录 fd，不为只读 check 创建文件。
    lock = os.open('/sys/class/net/' + info['interface'] + '/device', os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = read_eeprom(info['interface'])
        after = candidate(before)
        if read_eeprom(info['peer']) != before:
            raise ValueError('两口 EEPROM 读回不一致，停止')
        state = '配置已修复' if before == after else '匹配已验证的 Port0 MDI 配置问题'
        print(f"{info['interface']} / {info['peer']} ({info['pci']}): {state}")
        print(f'EEPROM SHA256: {sha(before)}')
        if values['action'] != 'check':
            if values['action'] == 'rollback':
                after = rollback_target(values['restore_dir'], info, before)
            if after == before:
                print('无需写入。若尚未完全断电，请按文档完成持久性验证。')
            else:
                if not argv and input('将备份并写入最小配置。输入 APPLY 确认: ').strip() != 'APPLY':
                    raise ValueError('未确认，取消写入')
                transact(info, before, after, values['backup_dir'], values['action'])
        else:
            print('只读检查完成；不据此判断链路、PHY 运行状态或断电持久性。')
    finally:
        os.close(lock)
    if not argv:
        save_config(values)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        print(f'停止：{error}', file=sys.stderr)
        sys.exit(1)
