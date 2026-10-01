<?php

namespace App;

use Symfony\Bundle\FrameworkBundle\Kernel\MicroKernelTrait;
use Symfony\Component\HttpKernel\Kernel as BaseKernel;

/** 以 Symfony FrameworkBundle 載入明確的 API 路由與服務，避免框架預設改變共用契約。 */
class Kernel extends BaseKernel
{
    use MicroKernelTrait;
}
