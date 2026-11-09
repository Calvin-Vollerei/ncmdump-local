# 贡献者 / Contributors

本项目由以下人员共同促成：

| 贡献者 | 角色 | 贡献 |
| --- | --- | --- |
| **Calvin-Vollerei**（[@Calvin-Vollerei](https://github.com/Calvin-Vollerei)） | 作者、维护者 | 项目全部代码、界面与发布 |
| **Gvndam**（[@Gvndam](https://github.com/Gvndam)） | 问题报告 | 报告了音频起点偏移导致输出 `.bin` 无法播放的容器解析缺陷，该问题在 **v1.0.2** 修复 |

## 关于 Gvndam 的报告

早期版本按固定偏移量定位音频起点，而真实 `.ncm` 文件在封面区之后存在两种相差 4 字节的
布局，导致部分文件解出的音频是一段无法播放的噪声，并被写成 `.bin`。

修复没有采用任何一种固定公式，而是**在两个候选偏移处各解密前 32 字节，用解出的音频签名
判断真正的起点**；认不出结果时不再写出 `.bin`，也不会删除源文件。

这类"只在某些文件上复现"的反馈很难靠自测发现，报告本身直接指向了出问题的那一行。

## 感谢

也感谢所有在 [Issues](https://github.com/Calvin-Vollerei/ncmdump-local/issues) 里反馈问题的用户。

---

## Contributors (English)

| Contributor | Role | Contribution |
| --- | --- | --- |
| **Calvin-Vollerei** ([@Calvin-Vollerei](https://github.com/Calvin-Vollerei)) | Author, maintainer | All code, UI and releases |
| **Gvndam** ([@Gvndam](https://github.com/Gvndam)) | Bug report | Reported the container-parsing defect where a 4-byte offset difference made the output an unplayable `.bin`; fixed in **v1.0.2** |

The fix does not trust either candidate offset: it decrypts the first 32 bytes at both and
picks the one whose head is a recognisable audio container. When neither matches, no `.bin` is
written and the source file is kept.

Thanks as well to everyone who reported problems in the issues.
