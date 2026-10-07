/**
 * 元数据（枚举 + 中文标签）缓存。
 *
 * 后端 /meta/enums 的 label 就是枚举值本身，真正的中文在 /meta/labels，
 * 所以这里把两者合并成一个 label() 查询函数，页面就不用关心来源。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { metaApi } from '@/api'
import type { EnumOption, MetaEnums, MetaLabels } from '@/api/types'

const EMPTY_ENUMS: MetaEnums = {
  project_type: [],
  project_status: [],
  subsystem_type: [],
  device_category: [],
  device_status: [],
  quote_status: [],
  fee_base: [],
  auxiliary_base: [],
  item_source: [],
  labor_class: [],
  quota_category: [],
  role: [],
}

export const useMetaStore = defineStore('meta', () => {
  const enums = ref<MetaEnums>({ ...EMPTY_ENUMS })
  const labels = ref<MetaLabels>({})
  const loaded = ref(false)

  async function load(force = false): Promise<void> {
    if (loaded.value && !force) return
    const [e, l] = await Promise.all([metaApi.enums(), metaApi.labels()])
    enums.value = e
    labels.value = l
    loaded.value = true
  }

  /** 中文标签：优先查 /meta/labels，查不到就退回枚举值本身。 */
  function label(group: keyof MetaLabels | string, value: string | null | undefined): string {
    if (!value) return '—'
    return labels.value[group]?.[value] ?? value
  }

  /** 某个枚举组的下拉选项（value 用于提交，label 用于展示）。 */
  function options(group: keyof MetaEnums): EnumOption[] {
    const list = enums.value[group] ?? []
    return list.map((o) => ({ value: o.value, label: label(group, o.value) || o.label }))
  }

  /** 设备状态的 Element Plus tag 类型，用于停产/临近停产着色。 */
  function deviceStatusTag(status: string): 'success' | 'warning' | 'danger' | 'info' {
    if (status === 'on_sale') return 'success'
    if (status === 'near_eol') return 'warning'
    if (status === 'eol' || status === 'discontinued') return 'danger'
    return 'info'
  }

  function isDiscontinued(status: string): boolean {
    return status === 'eol' || status === 'discontinued'
  }

  return { enums, labels, loaded, load, label, options, deviceStatusTag, isDiscontinued }
})
