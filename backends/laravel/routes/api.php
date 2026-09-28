<?php

use App\Http\Controllers\ArticleController;
use App\Http\Controllers\AuthController;
use App\Http\Controllers\CommentController;
use App\Http\Controllers\RegisterController;
use Illuminate\Support\Facades\Route;

Route::post('/auth/register', RegisterController::class);

Route::post('/auth/login', [AuthController::class, 'login']);
Route::post('/auth/logout', [AuthController::class, 'logout']);
Route::get('/me', [AuthController::class, 'me']);

Route::get('/articles', [ArticleController::class, 'index']);
Route::post('/articles', [ArticleController::class, 'store']);
Route::get('/articles/{article_id}', [ArticleController::class, 'show']);
Route::get('/me/articles', [ArticleController::class, 'mine']);

Route::get('/articles/{article_id}/comments', [CommentController::class, 'index']);
Route::post('/articles/{article_id}/comments', [CommentController::class, 'store']);
Route::get('/me/comments', [CommentController::class, 'mine']);
Route::get('/comments/{comment_id}', [CommentController::class, 'show']);

Route::put('/articles/{article_id}', [ArticleController::class, 'update']);
Route::put('/articles/{article_id}/visibility', [ArticleController::class, 'visibility']);
Route::put('/comments/{comment_id}', [CommentController::class, 'update']);
Route::put('/comments/{comment_id}/visibility', [CommentController::class, 'visibility']);
