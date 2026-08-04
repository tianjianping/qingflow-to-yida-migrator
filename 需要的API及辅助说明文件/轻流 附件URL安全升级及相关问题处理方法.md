# 功能描述
为了提高轻流系统附件安全性，匹配合规要求，轻流于11月14日晚对附件字段URL安全性进行升级：附件字段URL统一进行加签处理，URL有效期为24小时；超过24小时之后，URL失效，需要重新获取。

**<font style="color:rgb(5, 7, 59);">示例调整</font>****<font style="color:rgb(5, 7, 59);background-color:rgb(253, 253, 254);">：</font>**

<font style="color:rgb(5, 7, 59);background-color:rgb(253, 253, 254);">原URL：</font>https://{域名}/file/private/documents/default/afb197df-d22c-4b56-8642-60016e6bb0ef.png

<font style="color:rgb(5, 7, 59);background-color:rgb(253, 253, 254);">调整后URL：</font>https://{域名}/file/private/documents/default/afb197df-d22c-4b56-8642-60016e6bb0ef.png?qingflow-expire-time=1730800649&signature=5b55297445c1ccb47c25960b9d53df684655dc371f386aba33c8a44a544563e2&qingflow-storage-flag=&qingflow-auth_type=ANONYMOUS

# **<font style="color:rgb(38, 38, 38);"></font>**影响范围
## <font style="color:rgb(5, 7, 59);">轻代码模块及附件导出功能</font>
<font style="color:rgb(5, 7, 59);">在轻代码模块（如qlinker、webhook、openApi、qsource、代码块、提醒推送插件）或附件导出功能中，若使用到附件字段，当附件URL过期后，需触发重新生成附件URL。</font>

```plain
openApi，qlinker，webhook，代码块中，附件字段传递的url均为加签的url。
这些加签的url的有效期是24小时，过去之后，需要重新获取（有效期也是24小时）。
```

## <font style="color:rgb(5, 7, 59);">数据详情页附件URL</font>
打开数据详情页附件URL如果超过有效期，附件下载会提示，用户刷新页面或重新打开详情页操作即可。

## <font style="color:rgb(5, 7, 59);">数据导入导出</font>
```plain
导出的excel文件中，附件的url也是加签的，有效期24小时。
导出的excel文件附件的内容仍然可以导入。
```

注：

1.安全升级前上传的附件对应的URL使用不受影响。

2.附件字段EXCEL导入功能不受影响。

# 第三方保存附件内容推荐办法
## 获取到现成的URL如何转化为自己的永久URL？
第三方系统获取到附件字段的url之后，可以通过GET方法直接请求url，获取附件的内容。由于附件字段的url存在有效期，用户可以在获取附件内容之后，将附件上传至自己的存储系统，生成新的url去访问（用户可以根据自己的安全需要设置url的有效期）。

这里推荐主流厂商的对象存储系统，用户可以很方便的通过手动或者编码的方式上传文件并且生成访问此文件的url：

阿里云OSS：[https://help.aliyun.com/zh/oss/](https://help.aliyun.com/zh/oss/)

腾讯云COS：[https://cloud.tencent.com/document/product/436](https://cloud.tencent.com/document/product/436)

华为云OBS：[https://support.huaweicloud.com/obs/index.html](https://support.huaweicloud.com/obs/index.html)

AWS S3：[https://aws.amazon.com/cn/s3/](https://aws.amazon.com/cn/s3/)

云厂商的文档中提供了丰富的示例和代码Demo。

## 如何获取指定数据的指定附件字段的URL？
在确定的了数据的applyId和附件字段的queId的情况下，可以通过调用轻流的openApi来获取附件的url。

涉及到的openApi接口：

[https://exiao.yuque.com/ixwxsb/cqfg2y/wsag40n60hzhosgp](https://exiao.yuque.com/ixwxsb/cqfg2y/wsag40n60hzhosgp)（可以获取到字段的queId）

[https://exiao.yuque.com/ixwxsb/cqfg2y/emtsdcm7uo3xe11m](https://exiao.yuque.com/ixwxsb/cqfg2y/emtsdcm7uo3xe11m)（可以获取到某条数据各字段的值）
