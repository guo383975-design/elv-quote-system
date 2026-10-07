<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { metaApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const formRef = ref()
const loading = ref(false)
const errorText = ref('')
const apiOnline = ref<boolean | null>(null)

// 演示账号提示只在开发构建里出现。生产包里带着「admin / admin12345」
// 等于把可用口令印在登录页上（种子账号在生产环境仍可能存在）。
const isDev = import.meta.env.DEV

const form = reactive({
  username: '',
  password: '',
})

const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
}

onMounted(async () => {
  // 探活：后端没起来时给出明确提示，而不是让用户对着「用户名或密码错误」猜
  try {
    await metaApi.health()
    apiOnline.value = true
  } catch {
    apiOnline.value = false
  }
})

async function submit(): Promise<void> {
  errorText.value = ''
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return

  loading.value = true
  try {
    await auth.login(form.username.trim(), form.password)
    ElMessage.success(`欢迎回来，${auth.displayName}`)
    const redirect = route.query.redirect
    await router.replace(typeof redirect === 'string' && redirect ? redirect : { name: 'projects' })
  } catch (err) {
    errorText.value = extractErrorMessage(err)
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <div class="login-card">
      <div class="login-brand">
        <el-icon :size="26"><Lightning /></el-icon>
        <div>
          <h1>智能化弱电报价系统</h1>
          <p>XX科技有限公司 · 公司内网私有部署</p>
        </div>
      </div>

      <el-alert
        v-if="apiOnline === false"
        type="error"
        :closable="false"
        show-icon
        class="mb"
        title="无法连接后端服务"
        description="请确认 API 已启动（默认 http://127.0.0.1:8000），并检查 vite 代理配置。"
      />

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        size="large"
        @keyup.enter="submit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="用户名或邮箱" clearable>
            <template #prefix><el-icon><User /></el-icon></template>
          </el-input>
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="密码">
            <template #prefix><el-icon><Lock /></el-icon></template>
          </el-input>
        </el-form-item>

        <el-alert
          v-if="errorText"
          type="error"
          :closable="false"
          show-icon
          class="mb"
          :title="errorText"
        />

        <el-button type="primary" class="submit" :loading="loading" @click="submit">
          登 录
        </el-button>
      </el-form>

      <div v-if="isDev" class="hint">
        <p>本地开发：<code>cp apps/api/.env.example apps/api/.env</code>，再跑
          <code>python -m scripts.seed</code>，可用演示账号
          <code>admin / admin12345</code>。</p>
        <p>账号由管理员创建（<code>POST /api/v1/users</code>）。自助注册默认关闭，
          仅在库为空**且**后端配置了 <code>BOOTSTRAP_TOKEN</code> 时可用。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background: linear-gradient(150deg, #14243d 0%, #1e3355 55%, #1668dc 140%);
}

.login-card {
  width: 400px;
  padding: 28px 30px 22px;
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 18px 48px rgba(10, 22, 40, 0.35);
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 22px;
  color: var(--c-navy);
}

.login-brand h1 {
  margin: 0;
  font-size: 17px;
  font-weight: 700;
}

.login-brand p {
  margin: 3px 0 0;
  font-size: 12px;
  color: var(--c-muted);
}

.submit {
  width: 100%;
}

.mb {
  margin-bottom: 16px;
}

.hint {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px dashed var(--c-border);
  font-size: 12px;
  color: var(--c-muted);
  line-height: 1.7;
}

.hint p {
  margin: 0;
}

.hint code {
  padding: 1px 4px;
  background: #f5f7fa;
  border-radius: 3px;
  font-size: 11.5px;
}
</style>
