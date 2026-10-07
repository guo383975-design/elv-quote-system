<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <div class="nf">
    <el-result icon="warning" title="404" sub-title="页面不存在或已被移除">
      <template #extra>
        <el-button type="primary" @click="router.replace({ name: 'projects' })">
          返回项目列表
        </el-button>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.nf {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
}
</style>
