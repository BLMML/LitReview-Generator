# 文献手稿 / M12 design system

用户指定 ui-ux-pro-max **Anti-Polish / Raw Aesthetic**。已核对 styles.csv 的准确条目 `anti-polish-raw-aesthetic`。通用 design-system 搜索返回 Swiss Modernism / portfolio，不适用本产品，因此不持久化其建议。

研究工作区采用手稿、剪贴和档案账本的视觉。Paper White #FAFAF8；Pencil Grey #4A4A4A；Marker Black #1A1A1A；Kraft Brown #C4A77D。深红用于失败和需注意；墨绿用于完成。状态同时使用文字。

系统中文黑体负责操作和正文；宋体用于手稿标题；等宽字体用于编号和版本。无外部字体请求。纸纹以本地 SVG turbulence 实现；手绘划线、微倾斜标签、胶带贴片作为装饰，正文与表单保持正向。无平滑过渡、无 jitter、无循环动画。尊重 reduced-motion。

桌面：档案侧栏 + 文献/分析工作区 + 证据原文；375px：单列、可折叠证据，主导航水平换行。44px 控件，明确标签，3px 可见焦点，跳过导航链接。每屏一个主操作，任务期间仍能查阅和取消；未保存、历史版本、过时与待审不得仅依靠颜色。

Vue 3 Composition API + TypeScript + Pinia。页面统一编排 API 与轮询，组件只接收 props / events；PDF.js 从同源授权接口获取完整文件。
