<script setup lang="ts">
import { onMounted } from 'vue'

import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'

const auth = useAuthStore()
const meta = useMetaStore()

// 元数据（枚举 + 中文标签）全局只拉一次；失败不阻断，页面各自兜底。
onMounted(async () => {
  if (!auth.isLoggedIn) return
  try {
    await meta.load()
  } catch {
    // 忽略：接口挂了的话页面自己的请求也会报错提示
  }
})
</script>

<template>
  <router-view />
</template>
