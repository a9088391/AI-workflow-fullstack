<?php

use App\Http\Controllers\RegisterController;
use Illuminate\Support\Facades\Route;

Route::post('/auth/register', RegisterController::class);
