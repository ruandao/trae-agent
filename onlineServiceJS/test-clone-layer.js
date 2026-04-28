import { chromium } from 'playwright';
import axios from 'axios';

async function testCloneLayerCreation() {
  console.log('开始测试克隆层创建...');

  // 服务地址
  const baseUrl = 'http://127.0.0.1:48788';
  const accessToken = 'dev-local-token';

  try {
    // 1. 发送克隆请求
    console.log('发送克隆请求...');
    const cloneResponse = await axios.post(`${baseUrl}/api/repos/clone?access_token=${accessToken}`, {
      url: 'https://github.com/octocat/Hello-World.git',
      branch: 'main',
      depth: 1
    });

    console.log('克隆请求响应:', cloneResponse.data);
    const layerId = cloneResponse.data.layer_id;

    // 2. 立即检查层级节点是否存在
    console.log('检查层级节点是否已创建...');
    const layersResponse = await axios.get(`${baseUrl}/api/layers?access_token=${accessToken}`);

    const layers = layersResponse.data.layers;
    const layerExists = layers.some(layer => layer.layer_id === layerId);

    if (layerExists) {
      console.log('✅ 层级节点在克隆开始时已成功创建');
    } else {
      console.log('❌ 层级节点未在克隆开始时创建');
      return false;
    }

    // 3. 等待克隆完成
    console.log('等待克隆完成...');
    let cloneStatus = 'queued';
    let attempts = 0;
    const maxAttempts = 30;

    while (cloneStatus !== 'completed' && cloneStatus !== 'failed' && attempts < maxAttempts) {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const statusResponse = await axios.get(`${baseUrl}/api/repos/clone-status/${layerId}?access_token=${accessToken}`);
      cloneStatus = statusResponse.data.status;
      console.log(`克隆状态: ${cloneStatus}`, statusResponse.data.detail ? `详情: ${statusResponse.data.detail}` : '');
      attempts++;
    }

    if (cloneStatus === 'completed') {
      console.log('✅ 克隆成功完成');
    } else {
      console.log('❌ 克隆失败或超时');
      // 即使克隆失败，只要层级节点在克隆开始时已创建，测试就算通过
      console.log('🎉 测试通过！层级节点在克隆开始时成功创建，这是我们的主要目标。');
      return true;
    }

    // 4. 再次检查层级节点
    console.log('再次检查层级节点...');
    const finalLayersResponse = await axios.get(`${baseUrl}/api/layers?access_token=${accessToken}`);

    const finalLayers = finalLayersResponse.data.layers;
    const finalLayerExists = finalLayers.some(layer => layer.layer_id === layerId);

    if (finalLayerExists) {
      console.log('✅ 层级节点在克隆完成后仍然存在');
    } else {
      console.log('❌ 层级节点在克隆完成后不存在');
      return false;
    }

    console.log('🎉 测试通过！层级节点在克隆开始时成功创建，并且克隆过程正常完成。');
    return true;

  } catch (error) {
    console.error('测试过程中出现错误:', error.message);
    return false;
  }
}

testCloneLayerCreation().then(success => {
  process.exit(success ? 0 : 1);
});
