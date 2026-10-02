export default defineNuxtConfig({
  ssr: false,
  app: {
    baseURL: '/nuxt/',
    head: {
      htmlAttrs: { lang: 'zh-Hant' },
      title: '文章集｜Nuxt',
      meta: [{ name: 'viewport', content: 'width=device-width, initial-scale=1' }],
    },
  },
  css: ['~/assets/site.css'],
  devtools: { enabled: false },
})
