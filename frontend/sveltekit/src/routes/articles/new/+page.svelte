<script>
  import AuthorPage from '../../[...slug]/+page.svelte';
</script>

<!-- 明確路由優先於文章 ID 路由，共用作者頁避免發文表單分叉。 -->
<AuthorPage />
