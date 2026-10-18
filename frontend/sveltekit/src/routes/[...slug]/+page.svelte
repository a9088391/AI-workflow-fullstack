<script>
  import { BASE } from '$lib/contracts.js';
</script>

<svelte:head><title>找不到頁面｜文章集</title></svelte:head>
<div class="state-panel not-found"><h1>找不到頁面<span class="title-dot">.</span></h1><p>此頁面尚未提供，或網址不正確。</p><a class="button" href={`${BASE}/`}>返回文章列表</a></div>
