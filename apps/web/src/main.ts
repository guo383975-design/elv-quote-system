import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'

import 'element-plus/dist/index.css'
import './styles/main.css'
// 打印样式放最后：里面有 @media print 块 + 高优先级 !important，
// 必须在 element-plus / main.css 之后引入才能压住默认样式。
import './styles/print.css'

const app = createApp(App)

// 全量注册图标：内网工具不在意首屏体积，换取模板里直接写 <el-icon><Box /></el-icon>。
for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

app.mount('#app')
