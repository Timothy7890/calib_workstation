<template>
  <section class="page">
    <div class="page-inner guide">
      <h1 class="page-title">使用说明</h1>
      <p class="page-desc">2D 手眼标定：求解头部 / 腰部 RGB 相机相对机器人 <code>torso_link</code> 的位姿（外参），同时存档来自 Orbbec SDK 的内参，不重新标定内参。</p>

      <h2>准备</h2>
      <ul>
        <li>把棋盘格刚性固定在持板手臂末端，整个标定过程中不能松动。</li>
        <li>确认没有其他程序在控制手臂（遥操作、示教等）。</li>
        <li>操作员站在急停可及的位置，全程监护。</li>
      </ul>

      <h2>启动</h2>
      <p>在机器人上执行 <code>calib_workstation/start.sh</code>，脚本会自动停止相机推流、启动采集与回放服务并打印本页面地址；结束后 Ctrl+C，推流自动恢复。</p>

      <h2>标定流程</h2>
      <ol>
        <li><b>相机与计划</b>：选相机位置（头/腰）、持板手臂、相机序列号（列表来自当前连接的 Orbbec）、采集计划。右侧会实时显示所选相机画面与棋盘格检出状态。</li>
        <li><b>接管与归位</b>：点「接管」→「协力拖动」手动把手臂拖到原点附近 →「接住保持」。原点偏差超过阈值时运行会被拒绝。</li>
        <li><b>自动采集</b>：机器人按计划依次到达各采样点，静止后拍照并记录关节角；未检出完整棋盘格的点会被拒绝并重试。可随时「立即停止」。</li>
        <li><b>求解</b>：填入方格边长（mm），点「开始求解」。</li>
        <li><b>结果与生效</b>：查看外参内点数与残差；满意则「确认生效并归档」，外参与当次使用的 SDK 内参分别写入本机器人的标定目录。</li>
      </ol>

      <h2>结果怎么判断</h2>
      <ul>
        <li>内点 ≥ 8、平移残差均值 &lt; 5 mm、旋转残差均值 &lt; 0.5° 通常可用。</li>
        <li>内点太少或残差大：增加姿态差异更大的采样点（转动角度、距离变化），确认棋盘格没松动、方格边长填写正确。</li>
      </ul>

      <h2>产物目录</h2>
      <pre class="mono">&lt;data_root&gt;/&lt;机器人编号&gt;/calibrations/
  extrinsic/&lt;head|waist&gt;/&lt;运行名&gt;/manifest.json + handeye_result_left.json
  intrinsic/&lt;head|waist&gt;/&lt;运行名&gt;/manifest.json + camera_intrinsics.json
  &lt;type&gt;/&lt;head|waist&gt;/active.json   # 当前生效指向</pre>
    </div>
  </section>
</template>

<style scoped>
.guide h2 {
  margin: 28px 0 10px;
  font-size: 16px;
  font-weight: 500;
}

.guide ul,
.guide ol {
  margin: 0;
  padding-left: 22px;
  line-height: 1.9;
  color: #333;
}

.guide p {
  line-height: 1.8;
  color: #333;
}

.guide pre {
  padding: 12px 14px;
  background: #f5f5f5;
  border-radius: 4px;
  line-height: 1.7;
}
</style>
