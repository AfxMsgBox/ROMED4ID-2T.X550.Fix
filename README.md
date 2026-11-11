# ROMED4ID-2T 板载 X550 网口失联修复

这份文档记录一台 ASRock Rack ROMED4ID-2T 在升级网卡固件后，板载 X550 Port0 无法建立链路的原因、最小修复方法和验证结果。另一端口能够建立 10G 链路。

**本例问题是 Port0 的 MDI 线对映射配置错误。修复只改变 NVM 中两个字节：映射值和对应 CRC8。没有重新刷写完整固件。**

本例已完成完全断电验证，两口均恢复 10Gbps 全双工；后续 TCP 接收吞吐约 9.41Gbps、UDP 约 9.50Gbps。该结论针对这一故障状态，不能据此认定所有 X550 失联都适用。

## 适用范围

| 项目 | 本例及脚本要求 |
|---|---|
| 主板 DMI board_name | ROMED4ID-2T |
| 网卡 | Intel X550-AT2，8086:1563 |
| 子系统 | ASRock Rack，1849:1563 |
| 驱动 | ixgbe |
| 固件 EEPID | 8000172D；ethtool 显示 0x8000172d, 1.3105.0 |
| 修复对象 | PCI function 0，即本例的 Port0；不依赖接口名称 |
| EEPROM 读回长度 | 16384 字节 |
| PHY 配置指针 | word 0x43 的值为 0x0FC2 |
| 配置段 | 必须完整匹配下文已验证的 20 字节模板 |

实际运行环境为 Debian 13 / PVE，内核 `7.0.14-28-pve`。发布脚本使用 Python 3 标准库、ethtool 和 iproute2；只读检查已在修复后的实际服务器通过。写入路径使用本例验证过的 ethtool 命令，发布脚本的分支采用合成 EEPROM 和模拟硬件调用测试，没有再次刷写正常设备，也没有声称在其他主板或内核上完成实机验证。

## 问题如何定位

1. Port0 无链路，Port1 为 10G。网线和交换机已经排除；完整断电及单独通告 100M、1G、10G 均未解决问题。
2. Port0 的寄存器、EEPROM 校验、中断、内部回环自检通过，链路测试失败。内部回环通过不能证明外部收发链路正常。
3. 读取 PHY 后发现，Port0 的 `1.E400 = 0x0006`，Port1 为 `0x0007`，相差 bit0，也就是 MDI direction。
4. 临时把 Port0 改成 `0x0007` 并重启自协商，5 秒内建立 10G；恢复 `0x0006` 后再次失联。Port1 始终正常。
5. NVM 的 PHY Config 段记录了这个错误值。修正段内 Port0 映射及 CRC8，完全断电后正确配置自动加载，两口均恢复 10G。

这说明本例与板上实际线对连接对应的 MDI 配置不一致。不能仅凭这一结果断言是哪个历史升级步骤造成，也不能将同一个位值推广到其他型号。

### PHY 全零读数的陷阱

本机内核的 ixgbe Clause 45 读取路径存在 addr/devad 交换现象，标准 MII 读取最初连正常端口也返回全零。补偿读取参数后，两口均读出 PHY ID `01540221`、PHY 固件 `020b`。所以全零不是“PHY 已损坏”的证据。

该现象来自本机实际驱动的检查，不是所有 ixgbe 版本的通用结论。写回调没有同样的交换，不能将读操作的补偿方式套用到写操作。发布脚本不进行 PHY 寄存器读写，避免依赖这个特定内核行为。

## 实际修改了什么

下表地址以 **16 位 word** 为单位；文件和 ethtool 的 offset 以 **字节** 为单位。

| word 地址 | 原值 | 修复值 | 含义 |
|---|---:|---:|---|
| 0x0FC5 | 0x0000 | 0x0001 | Port0 MDI direction |
| 0x0FCB | 0x6000 | 0x3D00 | 配置段 CRC8，存于高字节 |

完整配置段为 10 个 little-endian word，总计 20 字节：

```text
原始：0009 E400 0001 0000 0001 E400 0021 0001 0001 6000
修复：0009 E400 0001 0001 0001 E400 0021 0001 0001 3D00
```

CRC8 使用 poly `0x07`、init `0x00`，按 little-endian 字节顺序计算段内前 18 字节。原 CRC 为 `0x60`，修复后为 `0x3D`；标准测试向量 `123456789` 得到 `0xF4`。

写入范围为 byte offset **8074**、length **14**；实际改变的只有 byte **8074** 和 **8087**。Port1 映射、MAC、指针和其他 EEPROM 数据保持原样。脚本根据本机读取的内容生成候选，不复制其他机器的 EEPROM。

## Debian 下使用脚本

先安排维护窗口，从 BMC、控制台或其他网卡操作。修复和回退要求两个 X550 端口均未承担业务：不得加入网桥/bond、配置非链路本地地址、业务路由、上层 VLAN/macvlan 或 SR-IOV VF。脚本会检查这些条件，但不会替你拆除现有网络配置。

安装依赖并下载仓库：

```sh
sudo apt-get update
sudo apt-get install -y python3 ethtool iproute2 git
git clone https://github.com/AfxMsgBox/ROMED4ID-2T.X550.Fix.git
cd ROMED4ID-2T.X550.Fix
```

### 先只读检查

```sh
sudo python3 fix_x550.py --action check
```

脚本自动识别唯一匹配的 Port0；存在多个匹配设备时，需要显式传入 `--interface <Port0接口名>`。不能把 Port1 当作目标。硬件、固件或配置模板不符时会停止，没有 `--force` 绕过选项。

check 不写 EEPROM、不创建备份、不改变网络，也不检查运行时 PHY 或链路。它报告的是当前 NVM 读回状态，不能替代完全断电验证。

### 一条命令完成备份和修补

```sh
sudo python3 fix_x550.py --action apply --yes
```

脚本依次执行：核验硬件与配置 → 检查接口未承担业务 → 保存原始和候选 16KB 数据及元信息 → fsync 落盘并读回备份 → 再次核对设备内容 → 最小写入 → 两口完整读回比较。

默认备份位置为脚本目录下的 `backups/时间-随机串/`；可用 `--backup-dir /安全的备份目录` 指定。目录权限为 0700，文件为 0600。**备份是 ethtool 可读的 16KB EEPROM，不是完整 2MB Flash。** 其中可能包含本机 MAC，应另外保存，不要提交 GitHub。

已修复时跳过写入。写入或读回异常会停止并保存证据，不会盲目重试或自动整卡回刷；若是部分写入异常，需依据实际读回和备份另行处理，受限回退命令可能拒绝执行。

### 完全断电验证

写入成功不代表已经验证 Flash 持久性。ixgbe 的相关写入路径没有向上层传播所有校验/Flash 提交错误，因此必须读回并验证断电后的状态。

1. 正常关闭操作系统，再完全切断主板供电，包括待机电源；普通重启不能代替这一步。
2. 重新供电启动，再执行 `--action check`，应显示“配置已修复”。
3. 确认两口启用后 `ethtool <接口名>` 显示 `10000Mb/s`、`Full`、`Link detected: yes`。如果系统未配置自动启用，需先在维护方案内启用接口；管理 DOWN 不等于修复失败。
4. 在正确配置网络后进行实际收发测试。

脚本不自动关机、重启、修改网桥或写 PHY 运行寄存器。因此“一键”指备份和 NVM 修补，完全断电仍需手动完成。

### 受限回退

回退会恢复错误映射，不应在修复正常时执行。仅支持发布脚本自己生成的某次 apply 备份，且当前 EEPROM 必须完整等于该次候选：

```sh
sudo python3 fix_x550.py --action rollback \
  --restore-dir /安全的备份目录/某次备份子目录 --yes
```

脚本核对设备 PCI、两口 MAC、文件哈希及当前内容，再另建回退前备份，恢复同一 14 字节区间。设备或内容发生其他变化时拒绝覆盖；本例早期人工排查备份不是这个命令的输入格式。回退同样需要读回和手动断电验证。

### 交互与配置

不传参数时进入交互模式，选择操作和接口，写入前必须输入 `APPLY`。流程成功结束时询问是否保存配置，默认不保存；保存为同目录同名 `fix_x550.conf`，权限 0600。

传入任何参数后不再交互；apply/rollback 必须显式提供 `--yes`。参数优先级：交互输入 > 命令行 > 配置 > 默认值。配置仅在显式使用 `--config [PATH]` 时读取，省略 PATH 则读取同目录同名配置；写入授权 `--yes` 不保存，也不接受从配置读取。

```sh
python3 fix_x550.py --help
sudo python3 fix_x550.py --config --action check
```

配置是 JSON，只允许 `action`、`interface`、`backup_dir`、`restore_dir` 四个字符串字段，不需要密码。

## 仓库中的华擎参考固件

`firmware/ASRR_X550_0_0_002_0.bin` 是华擎下载包中的**未修改原始文件**，用于留存和对照，不是上述两个字节修复的替代品，脚本不使用它刷卡。

- 来源：[华擎 nvmupdate(3.15).zip](https://download.asrock.com/TSD/nvmupdate%283.15%29.zip)
- BIN 大小：2097152 字节；EEPID：`80001373`。
- BIN SHA256：`e6111be0d006679a5211d569066543011c9953cdd5225a52c11fe82fe0af3405`
- 来源 ZIP SHA256：`071b70cf0c19a7095fed4c286202b5a55a77037e7a077f98562990ee8e26208b`

尚未找到明确确认该镜像适配 ROMED4ID-2T 的官方说明；本例分析到的该包 Port0/Port1 MDI 配置也不等于这块板已验证的配置。**不要因为文件来自华擎就直接把它整卡刷入本板。**

源包的文件许可说明将 `*.bin` 归于 Intel firmware license，允许按条件原样分发。对应声明原样保存在 [firmware/Intel_firmware_license.txt](firmware/Intel_firmware_license.txt)。仓库不包含用户完整 Flash 备份、网卡 MAC、内网地址、排查日志或修改后的整卡固件。

## 验证结果与边界

2026-11-11 完全断电后，两口 NVM 配置仍正确、PHY 自动加载正确映射、链路为 10G 全双工。正向 iperf3 测试使用 MTU 1500，预热 3 秒、计量 20 秒：

| 端口 | TCP 单流 | TCP 8 流 | UDP 8 流接收 | 该 UDP 轮丢包率 |
|---|---:|---:|---:|---:|
| 修复口 Port0 | 9.41Gbps | 9.41Gbps | 9.50Gbps | 0.00030% |
| Port1 | 9.41Gbps | 9.41Gbps | 9.50Gbps | 0% |

每轮网卡错误计数未增加；这不代表 TCP 无重传，也不替代反向、双口同时满载或长期稳定性测试。

## 参考资料

- [ASRock Rack ROMED4ID-2T 产品页](https://www.asrockrack.com/general/productdetail.asp?Model=ROMED4ID-2T)
- [Intel X550 数据手册 Rev 2.7](https://cdrdv2-public.intel.com/333369/333369_X550_Datasheet_Rev2.7.pdf)：PHY、NVM 配置与更新相关章节。
- [Linux ixgbe ethtool 实现](https://github.com/torvalds/linux/blob/master/drivers/net/ethernet/intel/ixgbe/ixgbe_ethtool.c)：`ixgbe_set_eeprom` 及校验更新路径。上游实现会变化，操作前仍应核对实际使用的驱动。
