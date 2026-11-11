---
name: seedance-video-prompt
description: 为 Seedance 优先的 AI 视频制作首帧图片提示词和图生视频完整提示词。适用于根据分镜、故事、参考素材或单个镜头生成首帧静态画面、中文视频提示词、英文视频提示词、@素材引用说明、镜头运动、声音和模型迁移建议的场景。
---

# Seedance 首帧图片与图生视频提示词 Skill

默认以 Seedance 工作流交付，但同时输出完整中文和英文视频提示词。核心纪律：**首帧描述 0 秒可见状态；视频提示词才描述从该状态开始发生的运动、交互和结果。** 所有成品提示词默认无台词、无人物说话、无任何文字生成。

## 最少澄清信息

用户没有提供时，只询问最影响结果的 2-3 项：

1. 画幅、时长、目标端：即梦/豆包/火山引擎或其他模型。
2. 镜头中谁、在哪里、从什么状态开始，最终需要发生什么。
3. 是否有角色/场景/动作/声音参考素材，及每份素材的用途。

缺项时采用保守默认：单镜 **4-5 秒**、一个原创角色、一个场景、一个主要动作、无人物说话、无品牌文字或任何可读文字。用户明确指定时长时严格服从。不要把多个复杂行为塞进一个短镜。

## 成品提示词硬约束

每一条首帧和视频提示词均按以下顺序组织：

```text
主体：{角色/物体、外形、服装、姿态、固定道具}。
场景：{地点、时间、空间关系、光源、环境状态}。
```

随后再写动作、镜头、整体画面风格和声音。每镜同时写**景别**与**焦段**；景别是特写/中景/全景，`35mm`、`50mm`、`85mm` 是焦段，二者不可互相替代。

| 焦段 | 默认用途 |
|---|---|
| 24-28mm | 建立空间、强透视、动态环境。 |
| 35mm | 双人中景、走位、带环境的叙事；默认首选。 |
| 50mm | 单人中近景、自然的人物反应。 |
| 85mm | 近景、手部/物件特写、背景压缩。 |

每镜都加入整体画面风格块：

```text
整体画面风格：{媒介}，{角色/表情要求}，{光影、材质与色彩}，{成片品质}。
```

例如：`采用 Disney《魔法满屋》风格，高品质 3D 角色，超丰富面部表情，电影级动画品质。` 如果平台拦截 IP，将其改写为：`明亮拉美魔幻家庭动画美学，高品质 3D 角色，超丰富面部表情，饱和暖色与电影级动画品质。`

每镜必须以正向约束写明：`无台词，无人物说话，无旁白；不生成任何文字、字幕、招牌文字、logo、水印或可读符号。`

## 关键边界：首帧不是视频摘要

### 首帧的任务

首帧图片提示词只写视频起点可见的静态状态：

- 人物和物体的身份、服装材质、姿态、朝向、手的位置和空间距离。
- 动作的**准备阶段**或刚开始的瞬间，例如“正迈出第一步”“手臂刚抬起”“手指停在门把手前”。
- 构图、景别、可见光源、场景、风格和画幅。
- 必须保留到视频中解决的事情：递出、接住、打开、跌落、拥抱完成、转身离开、爆发、变形完成等。

### 视频的任务

视频提示词从首帧开始，按时序写清：主体动作、对方反应、一个镜头运动、关键声音和结束状态。起始描述必须与首帧一致，不能让角色在 0 秒瞬移到另一位置或已经完成动作。

### 正反例：递水

**视频意图**：男人拿着水走到女人面前，把水递给她。

| 类型 | 正确写法 | 错误写法 |
|---|---|---|
| 首帧图片 | 男人在画面左侧中景，右手拿一瓶未递出的水，正向坐在画面右侧的女人迈出一步；两人相距约两米，女人抬眼看他。 | 男人已经站在女人面前，水瓶已递到她手中。 |
| 视频 | 男人继续平稳走近，在女人面前停下，伸手递出水；女人抬手接住，短暂停顿后看向他。 | 从两人已完成递水的画面开始，再描述他们递水。 |

用同一原则处理“走进房间”：首帧应是角色站在门外、手在门把手或正迈向门槛；视频再写开门、跨过门槛、进入室内和室内反应。

## Seedance 素材规则

使用参考素材时，为每一项明确职责；不要只写“参考 @视频1”。可组合但不得含混：

| 素材 | 允许的清晰职责示例 |
|---|---|
| `@图片1` | 作为首帧；或只用于角色外形/服装。 |
| `@图片2` | 只作为场景、道具或尾帧参考。 |
| `@视频1` | 只参考运镜、动作编排、剪辑节奏或特效之一/数项。 |
| `@音频1` | 只参考 BGM、节拍或环境声；不得作为旁白/人声参考。 |

写明：`@图片1 作为首帧，锁定 P1 的服装和面部特征；@图片2 仅参考雨夜便利店场景；@视频1 仅参考缓慢侧向跟拍的镜头运动。`

不要上传或要求使用写实真人脸部素材；使用原创角色、风格化角色或合规的非真人参考素材。避免具体受保护 IP 名称、角色名、商标和不必要的文字生成需求。

## 提示词组装流程

### 1. 把分镜压缩为镜头合同

先提炼，必要时向用户确认：

```text
镜头目的：
首帧时刻（0 秒）：
主要动作（只能有一个）：
结束状态：
角色/道具一致性锚点：
画幅、时长、景别、焦段、镜头运动：
整体画面风格：
声音：
参考素材及职责：
```

如果主要动作包含“走近、递出、接住、转身离开”，拆为两个或更多镜头，或保留其中最重要的一个。

### 2. 写首帧图片提示词

使用下面的中文模板。首帧提示词不能出现未来时态、结果词或分时段描述。

```text
【首帧图片提示词】
主体：{角色外形、服装材质、固定道具与状态锚点}。
场景：{地点、时间、可见光源、空间层次}。
{画幅}，{景别、焦段（如 35mm / 50mm / 85mm）与构图}。
0 秒定格：{角色位置、朝向、姿态、手部状态、与其他人物/物体的距离}；
{动作预备或刚开始的可见瞬间}。
整体画面风格：{媒介、表情、光影、材质、色彩与成片品质}。无任何可读文字或符号。
```

图片提示词可以包含风格锚点，但优先保证姿势、空间和动作起点清楚；不要为了“电影感”加入与动作无关的爆炸、漂浮粒子或复杂特效。

### 3. 写 Seedance 中文视频提示词

先声明素材，再从首帧连续展开。5 秒通常只写一个动作；10 秒以上可按时间段写，时间段之间必须物理连续。

```text
【Seedance 中文图生视频提示词】
{素材引用及每项职责；无素材则删除本行}
主体：{角色/物体、服装、道具与状态锚点}。
场景：{地点、时间、光源与空间关系}。
以首帧画面为起点，保持 {角色、服装、道具、场景、光线} 全程一致。
0-{x} 秒：{从首帧姿势自然开始的主要动作}。
{x}-{时长} 秒：{对方/环境的单一反应，或动作的完成}。
镜头：{景别}，{焦段}，{唯一明确的镜头运动}；{必要时的对焦/速度规则}。
整体画面风格：{媒介、角色表情、光影、材质、色彩与成片品质}。
声音：{无配乐或明确的 BGM}；同期声包括 {2-4 个具体环境声音}，无台词、无旁白、无人声。
收束：{最后一帧的可见状态或留白}。{画幅、风格、色调}。不生成任何文字、字幕、logo、水印或可读符号。
```

针对 Seedance，优先使用肯定式、具体的画面目标。如需抑制常见问题，在正文里写“手部自然完整、皮肤与材质真实、画面稳定、无额外文字覆盖”等正向护栏；只有目标端明确提供独立负面框时才给逗号分隔的负面短语。

### 4. 写英文完整版视频提示词

英文版不是逐字机器翻译，必须与首帧和中文时间线一致。使用清晰、短句、按时间推进的完整描述：

```text
【English image-to-video prompt】
Subject: {identity, clothing, prop, pose, and continuity anchors}.
Scene: {location, time, lighting, spatial relationship, and environment}.
Starting exactly from the provided first-frame image, preserve the subject, scene, and composition.
{0-second state restated briefly}.
0-{x}s: {one continuous primary action}.
{x}-{duration}s: {one reaction or completion that follows naturally}.
Camera: {shot size}, {focal length}, {one camera movement}, {focus/speed if needed}.
Overall visual style: {medium, facial-expression requirement, lighting, materials, color, and finish}.
Sound: {music direction}. Diegetic environmental sound only: {specific sounds}; no dialogue, narration, or voices.
End on: {visible final state}. {aspect ratio and style constraints}. No text, subtitles, signs, logos, watermarks, or readable symbols.
```

动词要可视化：`walks closer`, `extends the bottle`, `accepts it`, `pauses`，不要用无可见行为的 `feels dramatic`、`becomes emotional`。

### 5. 给出设置与迁移建议

最后给一小段，不重复完整提示词：

- **Seedance**：说明建议时长、画幅、是否使用首帧、素材 `@` 职责，以及是否适合按时间段描述。
- **Veo/Kling 等**：保留英文完整版；只在平台确有独立负面框时提供负面短语。没有负面框的模型使用正向护栏，不写 `no ...` 命令。
- 提醒用户以当前产品界面的实际时长、素材数、审核和负面框能力为准；这些能力会随端和版本变化。

## 固定输出格式

对每个镜头严格按下面顺序输出：

1. `镜头合同`：目的、时长/画幅、首帧状态、主要动作、结束状态、素材职责。
2. `首帧意图说明`：解释首帧冻结的是哪个动作起点，以及为什么未写结果。
3. `首帧图片提示词`：中文完整可复制版本。
4. `Seedance 中文图生视频提示词`：完整可复制版本。
5. `English image-to-video prompt`：完整可复制版本。
6. `Seedance 素材与设置建议`：`@` 引用、时长、画幅、声音/负面框说明。
7. `连续性检查`：首帧、动作和结束状态是否相容；与前后镜头的角色/方向/道具是否接上。

## 完整工作示例：男人递水

**镜头合同**：9:16，5 秒，便利店门口的傍晚。主体是手持未递出矿泉水的男人与坐着的女人；场景是便利店门口的湿润地面和暖白灯。起点是男人向女人走近；主要动作是递水；结尾是女人接住水后抬眼。中景双人镜头，35mm。无参考素材。

**首帧意图说明**：冻结在男人迈向女人的第一步，水瓶仍在男人手中，两人尚未接触。因此视频仍有走近、停下、递出与接住的可见过程。

```text
【首帧图片提示词】
主体：一名穿深灰色棉布夹克、袖口略有褶皱的年轻男人，右手自然握着一瓶未开封的矿泉水；
一名穿米白色针织外套的年轻女人坐在长椅上，双手仍放在膝前。
场景：傍晚的便利店门口，暖白实景灯从后方照亮两人，湿润地面映出细小光点。
竖屏 9:16，中景双人构图，35mm 镜头。
0 秒定格：男人正向她迈出第一步，身体微微前倾，水瓶还贴近自己的右侧；
两人相距约两米，女人刚抬眼看向他，尚未伸手接水。便利店的暖白灯从后方
照亮两人，浅景深。整体画面风格：高品质写实 3D 角色，丰富但克制的面部表情，
真实自然的皮肤与布料质感，电影级动画品质。无任何可读文字、字幕、logo、水印或符号。
```

```text
【Seedance 中文图生视频提示词】
主体：穿深灰色棉布夹克、右手持未开封矿泉水的年轻男人；穿米白色针织外套的年轻女人。
场景：傍晚便利店门口，暖白实景灯与湿润地面。
以首帧画面为起点，保持男人的深灰色棉布夹克、右手中的未开封矿泉水、
女人的米白色针织外套、便利店门口的暖白灯和湿润地面全程一致。
0-2 秒：男人从首帧的第一步继续自然走近女人，在她面前停下。
2-5 秒：他平稳地伸出右手递出矿泉水；女人抬手接住水瓶，短暂停顿后抬眼看他。
镜头：中景双人镜头，35mm，轻微侧向跟拍男人并在递水时自然停稳，浅景深。
整体画面风格：高品质写实 3D 角色，丰富但克制的面部表情，真实自然的皮肤与布料质感，电影级动画品质。
声音：无配乐，仅保留同期声：远处车辆驶过、便利店门铃轻响、鞋底踩过湿地、
塑料瓶被接住时的轻微摩擦声。收束在两人短暂对视、女人手持水瓶的状态。
竖屏 9:16，暖冷对比的傍晚实景光，手部自然完整，画面稳定；无台词、无旁白、无人物说话，
不生成任何文字、字幕、招牌文字、logo、水印或可读符号。
```

```text
【English image-to-video prompt】
Subject: a young man in a dark gray cotton jacket holding an unopened water bottle in his right hand,
and a seated young woman in a cream knit cardigan.
Scene: a convenience-store entrance at dusk, warm practical light, and wet reflective pavement.
Starting exactly from the provided first-frame image, preserve the subject, scene, and composition.
0-2s: From his first step in the image, the man walks naturally toward the seated woman
and stops in front of her.
2-5s: He calmly extends the unopened bottle with his right hand. The woman raises her hand,
accepts it, pauses for a moment, and looks up at him.
Camera: medium two-shot, 35mm lens, a subtle lateral tracking move following the man, settling naturally
as the bottle is offered; shallow depth of field.
Overall visual style: high-quality realistic 3D characters, rich but restrained facial expressions,
natural skin and fabric detail, cinematic animation quality.
Sound: no music. Diegetic environmental sound only: a distant passing car, a soft convenience-store door chime,
shoes on wet pavement, and a faint plastic-bottle contact sound. End on their brief eye contact,
with the woman holding the bottle. Vertical 9:16, realistic evening practical lighting, stable
image, natural complete hands, no dialogue, narration, or voices; no text, subtitles, signs, logos,
watermarks, or readable symbols.
```

**Seedance 设置建议**：选择 5 秒、9:16、首帧图生视频。若上传素材，示例可改为：`@图片1 作为首帧并锁定两名角色与便利店场景；@音频1 仅参考环境氛围。` 不要把参考素材写成无用途的“参考”。

## 交付前自检

- [ ] 首帧只写 0 秒可见状态，不含动作完成或未来结果。
- [ ] 中文和英文视频提示词均从同一首帧状态开始，时序、人物、道具和结束状态一致。
- [ ] 单镜只有一个主要动作；复杂事件已拆镜。
- [ ] 每条提示词均先写主体、再写场景，并同时写明景别和焦段。
- [ ] 已加入整体画面风格；默认 4-5 秒，除非用户明确指定其他时长。
- [ ] 每个 `@` 素材都有明确职责，未使用的素材未被虚构引用。
- [ ] 有景别、焦段、一个镜头运动、明确环境声音和可见收束。
- [ ] 无台词、无旁白、无人物说话，不生成任何文字、字幕、招牌文字、logo、水印或可读符号。
- [ ] 采用原创/合规角色，不依赖写实真人脸、受保护 IP、商标或强制生成文字。
- [ ] 所有模型专属能力均标注为以当前产品端内界面为准。
